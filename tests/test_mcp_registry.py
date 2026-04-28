"""Tests for the MCP server registry (src/profiles/mcp_registry.py)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.profiles.mcp_registry import (
    MCP_SERVER_PATTERNS,
    McpRegistry,
    McpServerConfig,
    _on_mcp_server_changed,
    builtin_from_config,
    derive_server_id,
    load_from_vault,
    parse_server_markdown,
    register_mcp_server_handlers,
    render_server_markdown,
    vault_path_for,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------


class TestDeriveServerId:
    def test_system_scope(self):
        assert derive_server_id("mcp-servers/playwright.md") == (None, "playwright")

    def test_project_scope(self):
        assert derive_server_id("projects/myapp/mcp-servers/gmail.md") == (
            "myapp",
            "gmail",
        )

    def test_unrelated_path(self):
        assert derive_server_id("agent-types/coding/profile.md") is None
        assert derive_server_id("projects/foo/README.md") is None
        assert derive_server_id("mcp-servers/sub/nested.md") is None

    def test_non_md_file(self):
        assert derive_server_id("mcp-servers/playwright.json") is None

    def test_windows_separator(self):
        assert derive_server_id("mcp-servers\\playwright.md") == (None, "playwright")


class TestVaultPathFor:
    def test_system_scope(self):
        path = vault_path_for("/data", "playwright", None)
        assert path == "/data/vault/mcp-servers/playwright.md"

    def test_project_scope(self):
        path = vault_path_for("/data", "gmail", "personal")
        assert path == "/data/vault/projects/personal/mcp-servers/gmail.md"


# ---------------------------------------------------------------------------
# Markdown parse
# ---------------------------------------------------------------------------


STDIO_MD = """---
name: playwright
transport: stdio
description: Browser automation
command: npx
args:
  - "@anthropic/mcp-playwright"
env:
  PWDEBUG: "0"
---

# Playwright

Some notes.
"""

HTTP_MD = """---
name: my-api
transport: http
description: Internal API
url: https://api.internal/mcp
headers:
  Authorization: Bearer token
---

# My API
"""


class TestParseServerMarkdown:
    def test_stdio(self):
        result = parse_server_markdown(STDIO_MD)
        assert result.is_valid
        c = result.config
        assert c.name == "playwright"
        assert c.transport == "stdio"
        assert c.description == "Browser automation"
        assert c.command == "npx"
        assert c.args == ["@anthropic/mcp-playwright"]
        assert c.env == {"PWDEBUG": "0"}
        assert "Some notes." in c.notes

    def test_http(self):
        result = parse_server_markdown(HTTP_MD)
        assert result.is_valid
        c = result.config
        assert c.name == "my-api"
        assert c.transport == "http"
        assert c.url == "https://api.internal/mcp"
        assert c.headers == {"Authorization": "Bearer token"}

    def test_fallback_name(self):
        text = "---\ntransport: stdio\ncommand: ls\n---\n"
        result = parse_server_markdown(text, fallback_name="ls-server")
        assert result.is_valid
        assert result.config.name == "ls-server"

    def test_project_id_only_from_path(self):
        # project_id arg goes to config; frontmatter project_id is ignored.
        text = "---\nname: x\ntransport: stdio\ncommand: ls\nproject_id: ignored\n---\n"
        result = parse_server_markdown(text, project_id="real-project")
        assert result.config.project_id == "real-project"

    def test_missing_frontmatter(self):
        result = parse_server_markdown("# just a heading\n")
        assert not result.is_valid
        assert any("frontmatter" in e for e in result.errors)

    def test_missing_name(self):
        text = "---\ntransport: stdio\ncommand: ls\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid
        assert any("name" in e for e in result.errors)

    def test_invalid_transport(self):
        text = "---\nname: x\ntransport: ws\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid
        assert any("transport" in e for e in result.errors)

    def test_stdio_without_command(self):
        text = "---\nname: x\ntransport: stdio\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid
        assert any("command" in e for e in result.errors)

    def test_stdio_args_must_be_strings(self):
        text = "---\nname: x\ntransport: stdio\ncommand: npx\nargs: [1, 2]\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid

    def test_stdio_env_must_be_string_to_string(self):
        text = "---\nname: x\ntransport: stdio\ncommand: npx\nenv:\n  KEY: 42\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid

    def test_http_without_url(self):
        text = "---\nname: x\ntransport: http\n---\n"
        result = parse_server_markdown(text)
        assert not result.is_valid
        assert any("url" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Render + round-trip
# ---------------------------------------------------------------------------


class TestRenderServerMarkdown:
    def test_round_trip_stdio(self):
        original = McpServerConfig(
            name="pw",
            transport="stdio",
            description="Browser automation",
            command="npx",
            args=["@a/b", "--port", "9000"],
            env={"DEBUG": "1"},
            notes="Hand-written notes\nover two lines",
        )
        text = render_server_markdown(original)
        parsed = parse_server_markdown(text)
        assert parsed.is_valid
        c = parsed.config
        assert c.name == original.name
        assert c.transport == original.transport
        assert c.description == original.description
        assert c.command == original.command
        assert c.args == original.args
        assert c.env == original.env
        assert "Hand-written notes" in c.notes

    def test_round_trip_http(self):
        original = McpServerConfig(
            name="api",
            transport="http",
            description="API",
            url="https://x.example/mcp",
            headers={"X-Token": "abc"},
        )
        text = render_server_markdown(original)
        parsed = parse_server_markdown(text)
        assert parsed.is_valid
        c = parsed.config
        assert c.name == original.name
        assert c.transport == original.transport
        assert c.url == original.url
        assert c.headers == original.headers

    def test_minimal_stdio(self):
        # No args, env, description, notes — round-trip still works.
        original = McpServerConfig(name="ls", transport="stdio", command="ls")
        text = render_server_markdown(original)
        parsed = parse_server_markdown(text)
        assert parsed.is_valid
        assert parsed.config.command == "ls"
        assert parsed.config.args == []
        assert parsed.config.env == {}


# ---------------------------------------------------------------------------
# Adapter dict
# ---------------------------------------------------------------------------


class TestToAdapterDict:
    def test_stdio_minimal(self):
        c = McpServerConfig(name="x", transport="stdio", command="ls")
        assert c.to_adapter_dict() == {"command": "ls", "args": []}

    def test_stdio_with_env(self):
        c = McpServerConfig(name="x", transport="stdio", command="ls", args=["-la"], env={"K": "v"})
        assert c.to_adapter_dict() == {
            "command": "ls",
            "args": ["-la"],
            "env": {"K": "v"},
        }

    def test_http_minimal(self):
        c = McpServerConfig(name="x", transport="http", url="https://x")
        assert c.to_adapter_dict() == {"type": "http", "url": "https://x"}

    def test_http_with_headers(self):
        c = McpServerConfig(name="x", transport="http", url="https://x", headers={"A": "b"})
        assert c.to_adapter_dict() == {
            "type": "http",
            "url": "https://x",
            "headers": {"A": "b"},
        }


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


def _config(name: str, project_id: str | None = None, **kwargs) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport=kwargs.pop("transport", "stdio"),
        project_id=project_id,
        command=kwargs.pop("command", "ls"),
        **kwargs,
    )


class TestRegistryLookup:
    def test_get_system_only(self):
        r = McpRegistry()
        r.upsert(_config("pw"))
        assert r.get("pw") is not None
        assert r.get("pw", project_id="any") is r.get("pw")
        assert r.get("missing") is None

    def test_project_shadows_system(self):
        r = McpRegistry()
        sys_pw = _config("pw", description="system")
        proj_pw = _config("pw", project_id="myproj", description="project")
        r.upsert(sys_pw)
        r.upsert(proj_pw)

        assert r.get("pw").description == "system"
        assert r.get("pw", project_id="myproj").description == "project"
        assert r.get("pw", project_id="other").description == "system"

    def test_list_for_system_scope(self):
        r = McpRegistry()
        r.upsert(_config("a"))
        r.upsert(_config("b"))
        r.upsert(_config("c", project_id="myproj"))

        names = [c.name for c in r.list_for_scope(None)]
        assert names == ["a", "b"]

    def test_list_for_project_scope_includes_inherited(self):
        r = McpRegistry()
        r.upsert(_config("system-only"))
        r.upsert(_config("shared", description="system"))
        r.upsert(_config("shared", project_id="myproj", description="project-override"))
        r.upsert(_config("project-only", project_id="myproj"))

        listing = r.list_for_scope("myproj")
        names_to_desc = {c.name: c.description for c in listing}
        assert names_to_desc == {
            "system-only": "",
            "shared": "project-override",  # project shadows
            "project-only": "",
        }

    def test_list_for_project_excludes_other_projects(self):
        r = McpRegistry()
        r.upsert(_config("a", project_id="myproj"))
        r.upsert(_config("b", project_id="otherproj"))

        names = [c.name for c in r.list_for_scope("myproj")]
        assert names == ["a"]


class TestRegistryMutation:
    def test_remove_returns_false_when_absent(self):
        r = McpRegistry()
        assert r.remove("missing") is False

    def test_upsert_replaces(self):
        r = McpRegistry()
        r.upsert(_config("x", description="v1"))
        r.upsert(_config("x", description="v2"))
        assert r.get("x").description == "v2"

    def test_remove(self):
        r = McpRegistry()
        r.upsert(_config("x"))
        assert r.remove("x") is True
        assert r.get("x") is None

    def test_builtin_cannot_be_clobbered(self):
        r = McpRegistry()
        builtin = McpServerConfig(name="agent-queue", transport="http", url="http://x")
        r.set_builtin(builtin)

        with pytest.raises(ValueError):
            r.upsert(_config("agent-queue"))

    def test_builtin_cannot_be_removed(self):
        r = McpRegistry()
        r.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://x"))
        with pytest.raises(ValueError):
            r.remove("agent-queue")

    def test_set_builtin_replaces_previous(self):
        r = McpRegistry()
        r.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://1"))
        r.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://2"))
        assert r.get("agent-queue").url == "http://2"


# ---------------------------------------------------------------------------
# Vault scan
# ---------------------------------------------------------------------------


class TestLoadFromVault:
    def test_loads_system_and_project(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        proj_dir = vault / "projects" / "myproj" / "mcp-servers"
        sys_dir.mkdir(parents=True)
        proj_dir.mkdir(parents=True)

        (sys_dir / "playwright.md").write_text(STDIO_MD)
        (proj_dir / "gmail.md").write_text(
            "---\nname: gmail\ntransport: stdio\ncommand: gmail-mcp\n---\n"
        )

        registry = McpRegistry()
        errors = load_from_vault(registry, str(vault))

        assert errors == []
        assert registry.get("playwright") is not None
        assert registry.get("playwright").project_id is None
        assert registry.get("gmail", project_id="myproj") is not None
        assert registry.get("gmail", project_id="myproj").project_id == "myproj"

    def test_drops_existing_user_entries(self, tmp_path: Path):
        vault = tmp_path / "vault"
        (vault / "mcp-servers").mkdir(parents=True)
        (vault / "mcp-servers" / "new.md").write_text(
            "---\nname: new\ntransport: stdio\ncommand: ls\n---\n"
        )

        registry = McpRegistry()
        registry.upsert(_config("stale"))
        load_from_vault(registry, str(vault))

        assert registry.get("stale") is None
        assert registry.get("new") is not None

    def test_preserves_builtins_across_reload(self, tmp_path: Path):
        vault = tmp_path / "vault"
        (vault / "mcp-servers").mkdir(parents=True)

        registry = McpRegistry()
        registry.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://x"))
        load_from_vault(registry, str(vault))

        assert registry.get("agent-queue") is not None
        assert registry.get("agent-queue").is_builtin

    def test_malformed_file_does_not_kill_load(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        sys_dir.mkdir(parents=True)
        (sys_dir / "good.md").write_text("---\nname: good\ntransport: stdio\ncommand: ls\n---\n")
        (sys_dir / "bad.md").write_text("not yaml at all")

        registry = McpRegistry()
        errors = load_from_vault(registry, str(vault))

        assert any("bad.md" in e for e in errors)
        assert registry.get("good") is not None

    def test_empty_vault(self, tmp_path: Path):
        registry = McpRegistry()
        errors = load_from_vault(registry, str(tmp_path / "missing"))
        assert errors == []
        assert len(registry) == 0


# ---------------------------------------------------------------------------
# Builtin from config
# ---------------------------------------------------------------------------


class TestBuiltinFromConfig:
    def test_returns_entry_when_enabled(self):
        config = MagicMock()
        config.mcp_server.task_mcp_entry.return_value = {
            "agent-queue": {"type": "http", "url": "http://127.0.0.1:8081/mcp"},
        }
        builtin = builtin_from_config(config)
        assert builtin is not None
        assert builtin.name == "agent-queue"
        assert builtin.transport == "http"
        assert builtin.url == "http://127.0.0.1:8081/mcp"
        assert builtin.is_builtin is True

    def test_returns_none_when_disabled(self):
        config = MagicMock()
        config.mcp_server.task_mcp_entry.return_value = {}
        assert builtin_from_config(config) is None


# ---------------------------------------------------------------------------
# Watcher integration
# ---------------------------------------------------------------------------


def _make_change(rel_path: str, vault_root: str, op: str) -> VaultChange:
    return VaultChange(
        path=os.path.join(vault_root, rel_path),
        rel_path=rel_path,
        operation=op,
    )


class TestWatcherIntegration:
    @pytest.mark.asyncio
    async def test_create_event_loads_into_registry(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        sys_dir.mkdir(parents=True)
        (sys_dir / "pw.md").write_text("---\nname: pw\ntransport: stdio\ncommand: npx\n---\n")

        registry = McpRegistry()
        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "created")],
            registry=registry,
            vault_root=str(vault),
        )
        assert registry.get("pw") is not None

    @pytest.mark.asyncio
    async def test_modified_event_reparses(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        sys_dir.mkdir(parents=True)
        path = sys_dir / "pw.md"

        path.write_text("---\nname: pw\ntransport: stdio\ncommand: ls\n---\n")
        registry = McpRegistry()
        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "created")],
            registry=registry,
            vault_root=str(vault),
        )
        assert registry.get("pw").command == "ls"

        path.write_text("---\nname: pw\ntransport: stdio\ncommand: cat\n---\n")
        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "modified")],
            registry=registry,
            vault_root=str(vault),
        )
        assert registry.get("pw").command == "cat"

    @pytest.mark.asyncio
    async def test_deleted_event_removes(self, tmp_path: Path):
        vault = tmp_path / "vault"
        registry = McpRegistry()
        registry.upsert(_config("pw"))
        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "deleted")],
            registry=registry,
            vault_root=str(vault),
        )
        assert registry.get("pw") is None

    @pytest.mark.asyncio
    async def test_malformed_modify_keeps_previous(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        sys_dir.mkdir(parents=True)
        path = sys_dir / "pw.md"
        path.write_text("not yaml")

        registry = McpRegistry()
        registry.upsert(_config("pw", description="kept"))
        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "modified")],
            registry=registry,
            vault_root=str(vault),
        )
        # Previous entry retained (validation failure does not clobber).
        assert registry.get("pw").description == "kept"

    @pytest.mark.asyncio
    async def test_on_reload_hook_fires_for_touched_entries(self, tmp_path: Path):
        vault = tmp_path / "vault"
        sys_dir = vault / "mcp-servers"
        sys_dir.mkdir(parents=True)
        (sys_dir / "pw.md").write_text("---\nname: pw\ntransport: stdio\ncommand: ls\n---\n")

        registry = McpRegistry()
        seen: list[list[tuple[str | None, str]]] = []

        async def on_reload(touched):
            seen.append(touched)

        await _on_mcp_server_changed(
            [_make_change("mcp-servers/pw.md", str(vault), "created")],
            registry=registry,
            vault_root=str(vault),
            on_reload=on_reload,
        )
        assert seen == [[(None, "pw")]]

    def test_register_handlers_uses_both_patterns(self, tmp_path: Path):
        watcher = VaultWatcher(str(tmp_path))
        registry = McpRegistry()

        ids = register_mcp_server_handlers(
            watcher,
            registry,
            vault_root=str(tmp_path),
        )

        assert len(ids) == len(MCP_SERVER_PATTERNS)
        # Re-registration with same handler IDs replaces in-place.
        ids2 = register_mcp_server_handlers(
            watcher,
            registry,
            vault_root=str(tmp_path),
        )
        assert ids == ids2
        assert watcher.get_handler_count() == len(MCP_SERVER_PATTERNS)
