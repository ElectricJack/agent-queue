"""Selected class effort reaches actual task, pool and supervisor launches."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.intelligence_classes import IntelligenceClass
from src.sessions.harness_parser import Harness, parse_harness_markdown
from src.sessions.spec import SessionSpecBuilder


@pytest.fixture
def claude():
    source = Path(__file__).parents[1] / "src/sessions/default_harnesses/claude.md"
    parsed = parse_harness_markdown(source.read_text())
    assert parsed.is_valid
    return parsed.harness


def build(harness, *, level="medium", lifecycle="task", task_class=None,
          agent_class=None, fixed_model=None, mapping=None):
    classes = {
        value: IntelligenceClass(value, value, "", mapping or {
            "anthropic": {"model": "claude-opus-5", "thinking": value},
            "openai": {"model": "gpt-5", "reasoning_effort": "minimal" if value == "off" else value},
        })
        for value in ("off", "low", "medium", "high", "unsupported")
    }
    profile = SimpleNamespace(id="supervisor" if lifecycle == "named" else "worker",
                              default_class=level, model="fallback-model",
                              _agent_intelligence_class=agent_class,
                              _agent_model_override=fixed_model)
    builder = SessionSpecBuilder(SimpleNamespace(security=None), intelligence_classes=classes)
    kwargs = dict(profile=profile, harness=harness, work_dir="/wd", session_id="s1",
                  instance_token="i1", prompt="start")
    if lifecycle == "named":
        return builder.build_named_spec(project_id=None, **kwargs)
    if lifecycle == "pool":
        return builder.build_pool_spec(project=SimpleNamespace(id="p1", name="Project"),
                                       agent_id="a1", **kwargs)
    return builder.build_task_spec(task=SimpleNamespace(id="t1", project_id="p1",
                                                       intelligence_class=task_class), **kwargs)


@pytest.mark.parametrize("lifecycle", ["task", "pool", "named"])
@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_class_effort_reaches_claude_launch(claude, lifecycle, level):
    spec = build(claude, level=level, lifecycle=lifecycle)
    argv = spec.command
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--effort") + 1] == level


@pytest.mark.parametrize(("task_class", "agent_class", "expected"), [
    ("medium", None, "medium"),
    ("medium", "high", "high"),
])
def test_class_effort_precedence_survives_a_fixed_agent_model(
    claude, task_class, agent_class, expected,
):
    spec = build(claude, level="low", task_class=task_class,
                 agent_class=agent_class, fixed_model="claude-opus-5-pinned")
    assert spec.command[spec.command.index("--model") + 1] == "claude-opus-5-pinned"
    assert spec.command[spec.command.index("--effort") + 1] == expected


def test_off_disables_thinking_without_sending_an_invalid_effort(claude, monkeypatch):
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    off = build(claude, level="off")
    low = build(claude, level="low")
    assert off.env["MAX_THINKING_TOKENS"] == "0"
    assert "--effort" not in off.command
    assert "MAX_THINKING_TOKENS" not in low.env
    assert low.command[low.command.index("--effort") + 1] == "low"


def test_selected_effort_is_not_overridden_by_inherited_effort(claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")
    spec = build(claude, level="low")
    assert spec.env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"


def test_effort_requires_a_declared_harness_flag(claude, caplog, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)
    spec = build(replace(claude, effort_flag=""), level="high")
    assert "--effort" not in spec.command
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in spec.env
    assert "effort_flag" in caplog.text


def test_unknown_claude_thinking_value_is_not_passed_as_an_effort(claude, caplog):
    spec = build(claude, level="unsupported")
    assert "--effort" not in spec.command
    assert "unsupported" in caplog.text


@pytest.mark.parametrize("provider", ["codex", "gemini"])
def test_other_provider_thinking_fields_never_use_claude_flags(provider):
    harness = Harness(id=provider, command=provider, model_flag="--model")
    spec = build(harness, mapping={
        "openai": {"model": "gpt-5", "reasoning_effort": "high"},
        "google": {"model": "gemini-2.5-pro", "thinking_budget": 24576},
    })
    assert "--effort" not in spec.command
    assert "MAX_THINKING_TOKENS" not in spec.env


@pytest.mark.parametrize("lifecycle", ["task", "pool", "named"])
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh"])
def test_codex_class_reasoning_uses_its_config_override(lifecycle, effort):
    harness = Harness(id="codex", command="codex", model_flag="-m")
    spec = build(harness, lifecycle=lifecycle, mapping={
        "openai": {"model": "gpt-5", "reasoning_effort": effort},
    })
    assert spec.command[spec.command.index("-m") + 1] == "gpt-5"
    assert spec.command[spec.command.index("-c") + 1] == f'model_reasoning_effort="{effort}"'
    assert "--effort" not in spec.command
    assert "MAX_THINKING_TOKENS" not in spec.env


@pytest.mark.parametrize(("task_class", "agent_class", "expected"), [
    ("medium", None, "medium"), ("medium", "high", "high"),
])
def test_codex_effort_precedence_survives_a_fixed_model(task_class, agent_class, expected):
    harness = Harness(id="codex", command="codex", model_flag="-m")
    spec = build(harness, level="low", task_class=task_class,
                 agent_class=agent_class, fixed_model="gpt-5-pinned")
    assert spec.command[spec.command.index("-m") + 1] == "gpt-5-pinned"
    assert spec.command[spec.command.index("-c") + 1] == f'model_reasoning_effort="{expected}"'


def test_codex_off_class_uses_its_declared_minimal_reasoning():
    harness = Harness(id="codex", command="codex", model_flag="-m")
    spec = build(harness, level="off")
    assert spec.command[spec.command.index("-c") + 1] == 'model_reasoning_effort="minimal"'
    assert "MAX_THINKING_TOKENS" not in spec.env


@pytest.mark.parametrize("effort", ["off", "max", "arbitrary"])
def test_invalid_codex_reasoning_is_not_sent_to_the_cli(effort, caplog):
    harness = Harness(id="codex", command="codex", model_flag="-m")
    spec = build(harness, mapping={"openai": {"model": "gpt-5", "reasoning_effort": effort}})
    assert "-c" not in spec.command
    assert "Unsupported Codex reasoning effort" in caplog.text


@pytest.mark.parametrize(("harness_id", "command"), [
    ("custom-openai", "custom-cli"), ("codex", "custom-cli"),
])
def test_openai_mapping_does_not_invent_flags_for_other_commands(harness_id, command):
    harness = Harness(id=harness_id, command=command, model_flag="-m")
    harness = SimpleNamespace(**vars(harness), env_map=harness.env_map, provider="openai")
    spec = build(harness, level="high")
    assert spec.command[spec.command.index("-m") + 1] == "gpt-5"
    assert "-c" not in spec.command
