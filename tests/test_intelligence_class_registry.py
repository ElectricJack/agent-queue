"""Hot-reload of vault intelligence classes — no daemon restart to add a class.

Covers the registry itself, the vault-watcher wiring, the consumers that
must read it live (agent create/edit, session spec resolution), and the
doctor check that surfaces a malformed file.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.event_bus import EventBus
from src.intelligence_classes import load_intelligence_classes
from src.intelligence_classes.registry import (
    IntelligenceClassRegistry,
    register_intelligence_class_handlers,
)
from src.sessions.harness_parser import Harness
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes
from src.vault_watcher import VaultWatcher

GOOD = """---
id: spark-low
name: Spark Low
description: added at runtime
---

```json
{
  "anthropic": {"model": "claude-spark-1", "thinking": "low"},
  "openai": {"model": "gpt-spark", "reasoning_effort": "low"}
}
```
"""

MALFORMED = """---
id: spark-low
name: Spark Low
---

no fenced json block here
"""


def classes_dir(data_dir: str) -> Path:
    path = Path(data_dir) / "vault" / "intelligence-classes"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def data_dir(tmp_path):
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    return data_dir


@pytest.fixture
def registry(data_dir):
    registry = IntelligenceClassRegistry(load_intelligence_classes(data_dir))
    return registry


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


def test_registry_is_a_live_mapping(registry):
    assert "standard-medium" in registry
    assert dict(registry)["standard-medium"].id == "standard-medium"
    assert registry.get("nope") is None
    assert sorted(registry) == sorted(registry.snapshot())


def test_reload_picks_up_a_new_class_file(registry, data_dir):
    assert "spark-low" not in registry
    (classes_dir(data_dir) / "spark-low.md").write_text(GOOD, encoding="utf-8")

    assert registry.reload(data_dir) == []

    assert registry["spark-low"].mapping["anthropic"]["model"] == "claude-spark-1"


def test_reload_drops_a_deleted_class(registry, data_dir):
    (classes_dir(data_dir) / "fast-low.md").unlink()
    registry.reload(data_dir)
    assert "fast-low" not in registry


def test_reload_keeps_the_previous_entry_for_a_malformed_file(registry, data_dir):
    before = registry["fast-low"]
    (classes_dir(data_dir) / "fast-low.md").write_text(MALFORMED, encoding="utf-8")

    errors = registry.reload(data_dir)

    assert registry["fast-low"] is before, "a bad save must not take the class offline"
    assert errors and "fast-low.md" in errors[0]
    assert "intelligence-classes/fast-low.md" in registry.errors
    # Unrelated classes survive the bad file.
    assert "standard-medium" in registry


def test_reload_clears_a_previous_error_once_the_file_parses(registry, data_dir):
    path = classes_dir(data_dir) / "spark-low.md"
    path.write_text(MALFORMED.replace("id: spark-low", "id: spark-low\nbroken"), encoding="utf-8")
    registry.reload(data_dir)
    assert registry.errors

    path.write_text(GOOD, encoding="utf-8")
    assert registry.reload(data_dir) == []
    assert registry.errors == {}
    assert "spark-low" in registry


# ---------------------------------------------------------------------------
# Vault watcher wiring
# ---------------------------------------------------------------------------


async def _dispatch(watcher: VaultWatcher) -> None:
    await watcher.check()


@pytest.mark.asyncio
async def test_watcher_adds_modifies_and_removes_classes(registry, data_dir, tmp_path):
    vault_root = str(Path(data_dir) / "vault")
    watcher = VaultWatcher(vault_root=vault_root, poll_interval=0.0, debounce_seconds=0.0)
    register_intelligence_class_handlers(watcher, registry)
    await _dispatch(watcher)  # take the baseline snapshot

    path = classes_dir(data_dir) / "spark-low.md"
    path.write_text(GOOD, encoding="utf-8")
    await _dispatch(watcher)
    assert registry["spark-low"].mapping["openai"]["model"] == "gpt-spark"

    path.write_text(GOOD.replace("gpt-spark", "gpt-spark-2"), encoding="utf-8")
    await _dispatch(watcher)
    assert registry["spark-low"].mapping["openai"]["model"] == "gpt-spark-2"

    path.unlink()
    await _dispatch(watcher)
    assert "spark-low" not in registry


@pytest.mark.asyncio
async def test_watcher_keeps_previous_entry_on_a_malformed_save(registry, data_dir):
    vault_root = str(Path(data_dir) / "vault")
    watcher = VaultWatcher(vault_root=vault_root, poll_interval=0.0, debounce_seconds=0.0)
    register_intelligence_class_handlers(watcher, registry)
    await _dispatch(watcher)

    before = registry["fast-low"]
    (classes_dir(data_dir) / "fast-low.md").write_text(MALFORMED, encoding="utf-8")
    await _dispatch(watcher)

    assert registry["fast-low"] is before
    assert "intelligence-classes/fast-low.md" in registry.errors


# ---------------------------------------------------------------------------
# Consumers read the live registry
# ---------------------------------------------------------------------------


class _FakeDB:
    """Just enough of the DB for ``_validate_agent_settings``."""

    async def list_agents(self):
        return []

    async def get_profile(self, profile_id):
        return SimpleNamespace(id=profile_id, harness="claude", model="")


@pytest.fixture
def handler(data_dir):
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"), data_dir=data_dir
    )
    registry = IntelligenceClassRegistry(load_intelligence_classes(data_dir))
    builder = SessionSpecBuilder(config, intelligence_classes=registry)
    orch = SimpleNamespace(
        db=_FakeDB(),
        bus=EventBus(validate_events=False),
        session_spec_builder=builder,
        intelligence_classes=registry,
    )
    handler = CommandHandler(orch, config)
    handler.set_active_project(None)
    yield handler
    handler._current_scope = None


@pytest.mark.asyncio
async def test_agent_settings_accept_a_class_added_at_runtime(handler):
    fields = {"name": "w1", "profile_id": "worker", "intelligence_class": "spark-low"}
    _, error = await handler._validate_agent_settings(dict(fields))
    assert error == "Intelligence class 'spark-low' not found"

    (classes_dir(handler.config.data_dir) / "spark-low.md").write_text(GOOD, encoding="utf-8")
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)

    _, error = await handler._validate_agent_settings(dict(fields))
    assert error is None, "a class added to the vault must not need a daemon restart"


def test_session_spec_resolves_a_class_added_at_runtime(handler):
    builder = handler.orchestrator.session_spec_builder
    harness = Harness(id="claude", command="claude")
    profile = SimpleNamespace(default_class="spark-low", model="", id="worker")

    assert builder._resolve_class_config(profile, harness, None) == {}

    (classes_dir(handler.config.data_dir) / "spark-low.md").write_text(GOOD, encoding="utf-8")
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)

    assert builder._resolve_class_config(profile, harness, None)["model"] == "claude-spark-1"


@pytest.mark.asyncio
async def test_reload_config_helper_refreshes_the_registry(handler):
    (classes_dir(handler.config.data_dir) / "spark-low.md").write_text(GOOD, encoding="utf-8")
    result = await handler._reload_intelligence_classes()
    assert result["success"] is True
    assert result["errors"] == []
    assert "spark-low" in handler.orchestrator.intelligence_classes


@pytest.mark.asyncio
async def test_list_reports_whether_each_class_is_live(handler):
    (classes_dir(handler.config.data_dir) / "spark-low.md").write_text(GOOD, encoding="utf-8")

    result = await handler._cmd_list_intelligence_classes({})
    rows = {row["id"]: row for row in result["classes"]}
    assert rows["spark-low"]["loaded"] is False, "on disk but not yet in the live registry"
    assert rows["standard-medium"]["loaded"] is True

    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    result = await handler._cmd_list_intelligence_classes({})
    rows = {row["id"]: row for row in result["classes"]}
    assert rows["spark-low"]["loaded"] is True
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


async def _run_check(handler, config):
    check = default_registry().get("intelligence_classes.parse")
    return await check.run(DoctorContext(config=config, handler=handler))


@pytest.mark.asyncio
async def test_doctor_ok_when_every_class_parses(handler):
    result = await _run_check(handler, handler.config)
    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_doctor_warns_on_a_malformed_class_file(handler):
    (classes_dir(handler.config.data_dir) / "fast-low.md").write_text(MALFORMED, encoding="utf-8")
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)

    result = await _run_check(handler, handler.config)

    assert result.severity is Severity.WARN
    assert "intelligence-classes/fast-low.md" in result.data["files"]
    # The previous class stays launchable while the file is broken.
    assert "fast-low" in handler.orchestrator.intelligence_classes


@pytest.mark.asyncio
async def test_doctor_info_without_a_registry(handler):
    handler.orchestrator.intelligence_classes = None
    result = await _run_check(handler, handler.config)
    assert result.severity is Severity.INFO
