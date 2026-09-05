import pytest

from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.playbooks.services import PlaybookServices
from src.tools.registry import ToolRegistry

T = lambda n: {"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}  # noqa: E731


def _svc(all_tools, core):
    s = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    s.tool_registry.get_all_tools.return_value = all_tools
    s.tool_registry.get_core_tools.return_value = core
    return s


def test_allowed_filters_and_orders_and_strips_navigation():
    s = _svc([T("a"), T("b"), T("reply_to_user"), T("load_tools")], [])
    assert [t["name"] for t in s.node_tools(["b", "reply_to_user", "a"])] == ["b", "a"]


def test_none_is_no_tools():
    """Playbook V2 Package 0 §5.4: "missing" no longer means "everything".

    An undeclared policy used to hand a node the registry's full catalogue.
    That is the same default-open shape as an empty capability set meaning
    "all", which the spec forbids — a playbook that needs tools names them.
    """
    s = _svc([T("a"), T("b"), T("load_tools")], [])
    assert s.node_tools(None) == []


def test_none_with_real_registry_grants_nothing():
    services = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    services.tool_registry = ToolRegistry()
    assert services.node_tools(None) == []


def test_unknown_allowed_is_filtered_not_raised():
    """A policy is an allowlist: a name the registry does not know is simply
    not granted.  Raising turned a narrowing intent into a run-time failure."""
    assert _svc([T("a")], []).node_tools(["zzz"]) == []
    assert [t["name"] for t in _svc([T("a")], []).node_tools(["a", "zzz"])] == ["a"]


def test_empty_allowed_is_no_tools():
    assert _svc([T("a")], [T("a")]).node_tools([]) == []



# ---------------------------------------------------------------------------
# Orchestrator.playbook_services() factory
# ---------------------------------------------------------------------------


def test_orchestrator_playbook_services(tmp_path):
    from unittest.mock import MagicMock
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "w"), database_path=str(tmp_path / "t.db"),
                    data_dir=str(tmp_path / "d"))
    o = Orchestrator(cfg)
    with pytest.raises(RuntimeError, match="command handler"):
        o.playbook_services()
    o._command_handler = MagicMock()
    o._tool_registry = MagicMock()
    s = o.playbook_services()
    assert s.llm is o.llm and s.handler is o._command_handler and s.tool_registry is o._tool_registry

