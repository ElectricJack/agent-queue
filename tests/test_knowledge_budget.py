"""Hard aggregate caps include required text, tools, wrappers and output."""

import pytest

from src.config import AppConfig, KnowledgeContextConfig, load_config
from src.knowledge.budget import ContextBudget
from src.records.models import RecordError


def test_utf8_upper_bound_and_tool_schemas_are_accounted():
    budget = ContextBudget(input_tokens=1000, output_tokens=20, wrapper_tokens=10)
    result = budget.account("é" * 100, "文" * 10, tools=[{"name": "lookup"}])
    assert result["required_tokens"] > 200
    assert result["knowledge_tokens"] == 30
    assert result["total_tokens"] == result["required_tokens"] + 60
    assert result["method"] == "upper_bound_bytes"


def test_required_overflow_is_a_refusal_without_truncation():
    budget = ContextBudget(input_tokens=100, output_tokens=20, wrapper_tokens=10)
    text = "Required instructions " * 10
    result = budget.account(text)
    assert result["effective_knowledge_tokens"] == 0
    assert not result["fits"]
    assert result["diagnostic"] == "context.required_over_budget"
    with pytest.raises(RecordError, match="context.required_over_budget"):
        budget.enforce(text)


def test_operator_memory_limit_and_remaining_window_never_increase():
    config = AppConfig()
    config.memory.context_max_tokens = 125
    config.knowledge.context.max_tokens = 8000
    budget = ContextBudget.from_config(config, remaining_tokens=5000)
    assert budget.knowledge_tokens == 125
    assert budget.input_tokens == 5000
    assert budget.account("required" * 100)["effective_knowledge_tokens"] == 0


@pytest.mark.parametrize("window, expected", [
    (258400, 258400), (16000, 16000), (None, 131072), (0, 131072),
    (-1, 131072), (True, 131072), ("258400", 131072),
])
def test_automatic_input_ceiling_uses_only_a_valid_model_window(window, expected):
    config = AppConfig()
    assert config.knowledge.context.input_max_tokens is None
    assert not config.knowledge.validate()
    budget = ContextBudget.from_config(config, model_context_window=window)
    assert budget.input_tokens == expected


@pytest.mark.parametrize("operator, window, expected", [
    (32768, 258400, 32768), (300000, 258400, 258400), (0, 258400, 0),
    (32768, None, 32768),
])
def test_explicit_input_caps_and_model_windows_only_lower_allowances(operator, window, expected):
    config = AppConfig()
    config.knowledge.context.input_max_tokens = operator
    assert ContextBudget.from_config(config, model_context_window=window).input_tokens == expected
    assert ContextBudget.from_config(
        config, model_context_window=window, remaining_tokens=100,
    ).input_tokens == min(expected, 100)


@pytest.mark.parametrize("changes", [dict(input_tokens=-1), dict(knowledge_tokens=True),
                                    dict(method="chars_over_four")])
def test_invalid_budgets_are_rejected(changes):
    with pytest.raises(ValueError):
        ContextBudget(**changes)


def test_context_config_is_inert_and_parses_budget_and_bounded_supervisor_scope(tmp_path):
    config = AppConfig()
    assert not config.knowledge.context.enabled and not config.memory.enabled
    path = tmp_path / "config.yaml"
    path.write_text(
        "messaging_platform: none\ndatabase:\n  url: postgresql://test@localhost/test\n"
        "knowledge:\n  context:\n    input_max_tokens: 12000\n    max_tokens: 500\n"
        "    supervisor_project_ids: [p, q]\n    supervisor_query: incident\n"
    )
    loaded = load_config(str(path))
    assert isinstance(loaded.knowledge.context, KnowledgeContextConfig)
    assert loaded.knowledge.context.input_max_tokens == 12000
    assert loaded.knowledge.context.max_tokens == 500
    assert loaded.knowledge.context.supervisor_project_ids == ["p", "q"]
    assert not loaded.knowledge.context.enabled
    loaded.knowledge.context.discovery_max_items = 9
    assert any(e.field == "context.discovery_max_items" for e in loaded.knowledge.validate())


def test_automatic_input_ceiling_parses_null_and_rejects_invalid_operator_caps(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "messaging_platform: none\ndatabase:\n  url: postgresql://test@localhost/test\n"
        "knowledge:\n  context:\n    input_max_tokens: null\n"
    )
    config = load_config(str(path))
    assert config.knowledge.context.input_max_tokens is None
    assert not config.knowledge.validate()
    for invalid in (-1, True, "auto"):
        config.knowledge.context.input_max_tokens = invalid
        assert any(e.field == "context.input_max_tokens" for e in config.knowledge.validate())
