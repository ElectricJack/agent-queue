import pytest

from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.playbooks.services import PlaybookServices

T = lambda n: {"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}  # noqa: E731


def _svc(all_tools, core):
    s = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    s.tool_registry.get_all_tools.return_value = all_tools
    s.tool_registry.get_core_tools.return_value = core
    return s


def test_allowed_filters_and_orders_and_strips_navigation():
    s = _svc([T("a"), T("b"), T("reply_to_user"), T("load_tools")], [])
    assert [t["name"] for t in s.node_tools(["b", "reply_to_user", "a"])] == ["b", "a"]


def test_none_uses_core_tools():
    s = _svc([], [T("core"), T("load_tools")])
    assert [t["name"] for t in s.node_tools(None)] == ["core"]


def test_unknown_allowed_raises():
    with pytest.raises(ValueError, match="Unknown tool names"):
        _svc([T("a")], []).node_tools(["zzz"])


def test_empty_allowed_is_no_tools():
    assert _svc([T("a")], [T("a")]).node_tools([]) == []
