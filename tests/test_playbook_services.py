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


# ---------------------------------------------------------------------------
# Compiler: llm_config keys the LLMCallSpec cannot carry are warned about
# ---------------------------------------------------------------------------


def _merge(frontmatter):
    from src.playbooks.compiler import PlaybookCompiler

    return PlaybookCompiler._merge_frontmatter(
        {"nodes": {}},
        {"id": "pb", "triggers": [], "scope": "system", **frontmatter},
        "hash",
        1,
    )


def test_compiler_warns_on_ignored_llm_config_keys(caplog):
    with caplog.at_level("WARNING", logger="src.playbooks.compiler"):
        result = _merge({"llm_config": {"model": "m", "temperature": 0.3, "top_p": 1}})
    assert result["llm_config"]["temperature"] == 0.3  # preserved, just unused
    assert "temperature" in caplog.text
    assert "top_p" in caplog.text


def test_compiler_silent_on_supported_llm_config_keys(caplog):
    with caplog.at_level("WARNING", logger="src.playbooks.compiler"):
        _merge(
            {
                "transition_llm_config": {
                    "provider": "anthropic",
                    "model": "m",
                    "intelligence_class": "fast-low",
                    "max_tokens": 100,
                }
            }
        )
    assert "are ignored" not in caplog.text
