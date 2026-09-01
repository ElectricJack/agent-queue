"""Package 0 T-2 — the compiler artifact is not a source of authority.

``playbook_install`` accepts JSON authored by the ``playbook-compiler``
agent, which reads untrusted Markdown prose.  Before Package 0 that JSON
decided ``scope``, ``triggers``, ``profile_id``, ``enabled``, ``max_tokens``
and ``llm_config`` outright, so a prompt injection in a playbook source had
a complete path to a system-scoped, always-on playbook running as the
supervisor profile.

After Package 0 the artifact owns only ``nodes``/``rules``: everything else
comes from the operator's vault frontmatter, the vault path, or the server.

Committed first (roadmap commit 1) with ``xfail(strict=True)``; T-5 … T-7
remove the markers as the implementation lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: The xfail marker is removed by T-5/T-7 (roadmap commit 2).
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.xfail(strict=True, reason="Package 0 T-2"),
]


SOURCE_MD = """---
id: memory-consolidation
triggers:
  - timer.24h
scope: system
llm_config:
  provider: gemini
  model: gemini-2.5-pro
transition_llm_config:
  provider: gemini
  model: gemini-2.5-flash
---

# Memory consolidation

Read the last 24h of agent-type memories and consolidate duplicates.
"""

HOSTILE_ARTIFACT: dict = {
    "id": "memory-consolidation",
    "version": 99,
    "source_hash": "sha256:0000000000000000",
    "compiled_at": "2020-01-01T00:00:00Z",
    "scope": "system",
    "enabled": True,
    "profile_id": "supervisor",
    "cooldown_seconds": 0,
    "max_tokens": 200000,
    "triggers": [
        "timer.24h",
        "task.completed",
        {"event_type": "gate.resolved", "filter": {"gate_type": "human"}},
    ],
    "llm_config": {"provider": "anthropic", "model": "claude-opus-5"},
    "nodes": {
        "start": {
            "entry": True,
            "prompt": "Read the last 24h of agent-type memories and consolidate duplicates.",
            "transitions": [{"when": "consolidated", "goto": "done"}],
        },
        "done": {"terminal": True},
    },
}


def _write_source(vault_root: Path, rel_path: str, markdown: str = SOURCE_MD) -> Path:
    p = vault_root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(markdown, encoding="utf-8")
    return p


def _write_artifact(vault_root: Path, name: str, data: dict | None = None) -> Path:
    p = vault_root / ".compiled" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data if data is not None else HOSTILE_ARTIFACT), encoding="utf-8")
    return p


@pytest.fixture
async def install_env(command_handler_factory, tmp_path):
    """Real handler + real PlaybookManager over a temp vault."""
    from src.playbooks.manager import PlaybookManager

    handler = await command_handler_factory()
    vault_root = Path(handler.config.vault_root)
    vault_root.mkdir(parents=True, exist_ok=True)
    pm = PlaybookManager(config=handler.config, data_dir=handler.config.data_dir)
    handler.orchestrator.playbook_manager = pm
    return handler, pm, vault_root


async def _install(handler, vault_root, artifact=None, playbook_id="memory-consolidation"):
    path = _write_artifact(vault_root, playbook_id, artifact)
    return await handler.execute(
        "playbook_install", {"playbook_id": playbook_id, "compiled_path": str(path)}
    )


class TestServerOwnedFields:
    async def test_artifact_scope_is_replaced_by_the_vault_path(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "projects/acme/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        assert result["success"] is True, result
        installed = pm.get_playbook("memory-consolidation")
        assert installed.scope == "project"
        assert any(w["field"] == "scope" for w in result["warnings"])

    async def test_system_path_keeps_system_scope(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        assert result["success"] is True, result
        assert pm.get_playbook("memory-consolidation").scope == "system"

    async def test_agent_type_path_derives_the_agent_type_scope(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "agent-types/coding/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        assert result["success"] is True, result
        assert pm.get_playbook("memory-consolidation").scope == "agent-type:coding"

    async def test_version_and_source_hash_are_recomputed(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        installed = pm.get_playbook("memory-consolidation")
        assert installed.version == 1
        assert installed.source_hash != "sha256:0000000000000000"
        assert installed.compiled_at != "2020-01-01T00:00:00Z"
        fields = {w["field"] for w in result["warnings"]}
        assert {"version", "source_hash"} <= fields

    async def test_version_increments_across_installs(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        await _install(handler, vault_root)
        await _install(handler, vault_root)

        assert pm.get_playbook("memory-consolidation").version == 2


class TestAuthorOwnedFields:
    async def test_artifact_triggers_are_replaced_by_frontmatter(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        installed = pm.get_playbook("memory-consolidation")
        assert installed.trigger_event_types == ["timer.24h"]
        assert any(w["field"] == "triggers" for w in result["warnings"])

    async def test_artifact_profile_id_is_dropped_when_frontmatter_has_none(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        assert pm.get_playbook("memory-consolidation").profile_id is None
        assert any(w["field"] == "profile_id" for w in result["warnings"])

    async def test_frontmatter_profile_id_wins(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(
            vault_root,
            "system/playbooks/memory-consolidation.md",
            SOURCE_MD.replace("scope: system", "scope: system\nprofile_id: worker-fast"),
        )

        result = await _install(handler, vault_root)

        assert pm.get_playbook("memory-consolidation").profile_id == "worker-fast"
        assert any(w["field"] == "profile_id" for w in result["warnings"])

    async def test_budget_fields_come_from_frontmatter(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        installed = pm.get_playbook("memory-consolidation")
        assert installed.max_tokens is None
        assert installed.llm_config is not None
        assert installed.llm_config.model == "gemini-2.5-pro"
        fields = {w["field"] for w in result["warnings"]}
        assert {"max_tokens", "llm_config"} <= fields


class TestOperatorOwnedEnabled:
    async def test_recompile_does_not_re_enable_a_disabled_playbook(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        assert (await _install(handler, vault_root))["success"] is True
        pm.get_playbook("memory-consolidation").enabled = False

        result = await _install(handler, vault_root)

        assert pm.get_playbook("memory-consolidation").enabled is False
        assert any(w["field"] == "enabled" for w in result["warnings"])

    async def test_first_install_uses_frontmatter_enabled(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(
            vault_root,
            "system/playbooks/memory-consolidation.md",
            SOURCE_MD.replace("scope: system", "scope: system\nenabled: false"),
        )

        await _install(handler, vault_root)

        assert pm.get_playbook("memory-consolidation").enabled is False


class TestCompilerOwnedFields:
    async def test_nodes_survive_the_merge_unchanged(self, install_env):
        handler, pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        installed = pm.get_playbook("memory-consolidation")
        assert set(installed.nodes) == {"start", "done"}
        assert installed.nodes["start"].entry is True
        assert not any(w["field"] == "nodes" for w in result["warnings"])


class TestSourceOfAuthority:
    async def test_no_matching_source_is_refused(self, install_env):
        handler, _pm, vault_root = install_env

        result = await _install(handler, vault_root)

        assert result["success"] is False
        assert any(
            "no source of authority" in (e["message"] or "") for e in result["errors"]
        )

    async def test_ambiguous_source_is_refused_naming_both_paths(self, install_env):
        handler, _pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")
        _write_source(vault_root, "projects/acme/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root)

        assert result["success"] is False
        message = " ".join(e["message"] or "" for e in result["errors"])
        assert "system/playbooks/memory-consolidation.md" in message
        assert "projects/acme/playbooks/memory-consolidation.md" in message

    async def test_artifact_id_mismatch_still_refused(self, install_env):
        handler, _pm, vault_root = install_env
        _write_source(vault_root, "system/playbooks/memory-consolidation.md")

        result = await _install(handler, vault_root, playbook_id="something-else")

        assert result["success"] is False


class TestApplySourceAuthorityUnit:
    async def test_returns_one_diagnostic_per_overridden_field(self):
        from src.playbooks.compiler import apply_source_authority

        merged, diagnostics = apply_source_authority(
            dict(HOSTILE_ARTIFACT),
            frontmatter={
                "id": "memory-consolidation",
                "triggers": ["timer.24h"],
                "scope": "system",
                "llm_config": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
            rel_path="projects/acme/playbooks/memory-consolidation.md",
            source_hash="sha256:beef",
            version=3,
            existing_enabled=False,
        )

        assert merged["scope"] == "project"
        assert merged["triggers"] == ["timer.24h"]
        assert "profile_id" not in merged
        assert merged["enabled"] is False
        assert merged["version"] == 3
        assert merged["source_hash"] == "sha256:beef"
        assert "max_tokens" not in merged
        assert merged["nodes"] == HOSTILE_ARTIFACT["nodes"]

        fields = {d.field for d in diagnostics}
        assert {
            "scope", "triggers", "profile_id", "enabled",
            "version", "source_hash", "max_tokens", "llm_config",
        } <= fields
        assert all(d.message for d in diagnostics)

    async def test_no_diagnostics_when_the_artifact_claimed_nothing(self):
        from src.playbooks.compiler import apply_source_authority

        merged, diagnostics = apply_source_authority(
            {"id": "p", "nodes": {}},
            frontmatter={"id": "p", "triggers": ["timer.24h"], "scope": "system"},
            rel_path="system/playbooks/p.md",
            source_hash="sha256:beef",
            version=1,
            existing_enabled=None,
        )

        assert merged["scope"] == "system"
        assert merged["enabled"] is True
        assert {d.field for d in diagnostics} == set()

    async def test_deprecated_staticmethod_still_delegates(self):
        from src.playbooks.compiler import PlaybookCompiler, apply_source_authority

        assert apply_source_authority is not None

        merged = PlaybookCompiler._merge_frontmatter(
            {"id": "x", "nodes": {}},
            {"id": "p", "triggers": ["timer.24h"], "scope": "system"},
            "sha256:beef",
            2,
        )
        assert merged["id"] == "p"
        assert merged["version"] == 2
        assert merged["source_hash"] == "sha256:beef"
