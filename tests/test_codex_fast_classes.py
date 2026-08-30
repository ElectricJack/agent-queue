"""Codex fast defaults stay separate from OpenAI API model defaults."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import Harness, parse_harness_markdown
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes


@pytest.fixture
def codex():
    source = Path(__file__).parents[1] / "src/sessions/default_harnesses/codex.md"
    return parse_harness_markdown(source.read_text()).harness


def builder(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    return SessionSpecBuilder(
        SimpleNamespace(security=None),
        intelligence_classes=load_intelligence_classes(str(tmp_path)),
    )


def profile(level="low", *, pin=None):
    original = SimpleNamespace(id="triage", harness="claude", model="claude-haiku-4-5",
                               default_class=f"fast-{level}", effort="")
    agent = SimpleNamespace(harness="codex", model=pin, intelligence_class=None)
    return apply_agent_overrides(original, agent)


@pytest.mark.parametrize("lifecycle", ["task", "pool", "named"])
@pytest.mark.parametrize(("level", "effort"), [("off", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")])
def test_fast_codex_defaults_reach_every_launch_and_snapshot(tmp_path, codex, lifecycle, level, effort):
    specs = builder(tmp_path)
    effective = profile(level)
    kwargs = dict(profile=effective, harness=codex, work_dir="/wd", session_id="s1",
                  instance_token="i1", prompt="start")
    if lifecycle == "named":
        spec = specs.build_named_spec(project_id=None, **kwargs)
    elif lifecycle == "pool":
        spec = specs.build_pool_spec(project=SimpleNamespace(id="p", name="Project"), agent_id="a", **kwargs)
    else:
        spec = specs.build_task_spec(task=SimpleNamespace(id="t", project_id="p", intelligence_class=None), **kwargs)
    assert spec.command[spec.command.index("-m") + 1] == "gpt-5.6-luna"
    assert spec.command[spec.command.index("-c") + 1] == f'model_reasoning_effort="{effort}"'
    assert resolve_launch_settings(effective, codex, specs) == {
        "llm_provider": "openai", "model": "gpt-5.6-luna", "intelligence_class": f"fast-{level}",
    }


def test_agent_model_pin_still_wins_while_codex_class_controls_effort(tmp_path, codex):
    specs = builder(tmp_path)
    effective = profile("off", pin="operator-pinned-model")
    argv = specs._compose_argv(harness=codex, profile=effective, session_id="s",
                              resume_key=None, prompt=None, session_name="s", files=[])
    assert argv[argv.index("-m") + 1] == "operator-pinned-model"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="low"'


@pytest.mark.parametrize(("hid", "command"), [("custom-openai", "api-cli"), ("codex", "api-cli"), ("custom-openai", "codex")])
def test_other_openai_harnesses_keep_api_mapping(tmp_path, hid, command):
    specs = builder(tmp_path)
    harness = Harness(id=hid, command=command, model_flag="-m")
    harness = SimpleNamespace(**vars(harness), provider="openai", env_map={})
    cfg = specs._resolve_class_config(profile("off"), harness, None)
    assert cfg == {"model": "gpt-5-mini", "reasoning_effort": "minimal"}
    assert specs._resolve_model(profile("off"), harness, None) == "gpt-5-mini"


def test_other_providers_and_non_fast_tiers_are_unchanged(tmp_path):
    specs = builder(tmp_path)
    assert specs._resolve_model(profile(), Harness(id="claude", command="claude"), None) == "claude-haiku-4-5"
    assert specs._resolve_model(profile(), Harness(id="gemini", command="gemini"), None) == "gemini-2.5-flash"
    for cid, cls in specs._intelligence_classes.items():
        if not cid.startswith("fast-"):
            assert "codex" not in cls.mapping
            assert cls.mapping["openai"]["model"] == "gpt-5"


def write_class(tmp_path, cid, mapping):
    root = tmp_path / "vault" / "intelligence-classes"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{cid}.md"
    path.write_text(f'---\nid: {cid}\nname: Custom name\n---\n\n```json\n' + json.dumps(mapping) + '\n```\n')
    return path


@pytest.mark.parametrize(("level", "legacy_effort", "effort"), [("off", "minimal", "low"), ("low", "low", "low"), ("medium", "medium", "medium"), ("high", "high", "high")])
def test_legacy_fast_backfill_is_in_memory_and_preserves_other_settings(tmp_path, codex, level, legacy_effort, effort):
    cid = f"fast-{level}"
    mapping = {"openai": {"model": "gpt-5-mini", "reasoning_effort": legacy_effort},
               "anthropic": {"model": "custom-claude", "thinking": "custom"}}
    path = write_class(tmp_path, cid, mapping)
    original = path.read_bytes()
    cls = load_intelligence_classes(str(tmp_path))[cid]
    assert cls.mapping["codex"] == {"model": "gpt-5.6-luna", "reasoning_effort": effort}
    assert cls.mapping["openai"] == mapping["openai"]
    assert cls.mapping["anthropic"] == mapping["anthropic"]
    assert cls.name == "Custom name" and path.read_bytes() == original
    specs = SessionSpecBuilder(SimpleNamespace(security=None), intelligence_classes={cid: cls})
    assert specs._resolve_model(profile(level), codex, None) == "gpt-5.6-luna"


@pytest.mark.parametrize("override", [{"model": "custom-codex", "reasoning_effort": "high"}, {}, None])
def test_existing_codex_mapping_is_never_backfilled(tmp_path, override):
    mapping = {"openai": {"model": "gpt-5-mini", "reasoning_effort": "low"}, "codex": override}
    path = write_class(tmp_path, "fast-low", mapping)
    original = path.read_bytes()
    assert load_intelligence_classes(str(tmp_path))["fast-low"].mapping == mapping
    assert path.read_bytes() == original


@pytest.mark.parametrize("openai", [
    {"model": "custom-api-model", "reasoning_effort": "low"},
    {"model": "gpt-5-mini", "reasoning_effort": "high"},
    {"model": "gpt-5-mini", "reasoning_effort": "low", "custom": True},
])
def test_custom_openai_mapping_is_not_backfilled(tmp_path, openai):
    mapping = {"openai": openai}
    write_class(tmp_path, "fast-low", mapping)
    assert load_intelligence_classes(str(tmp_path))["fast-low"].mapping == mapping


@pytest.mark.parametrize("cid", ["custom-fast", "standard-low", "deep-low"])
def test_backfill_only_applies_to_bundled_fast_ids(tmp_path, cid):
    mapping = {"openai": {"model": "gpt-5-mini", "reasoning_effort": "low"}}
    write_class(tmp_path, cid, mapping)
    assert load_intelligence_classes(str(tmp_path))[cid].mapping == mapping


def test_custom_codex_slice_and_class_precedence_are_used(tmp_path, codex):
    specs = builder(tmp_path)
    for level in ("low", "medium", "high"):
        write_class(tmp_path, f"fast-{level}", {"codex": {"model": f"codex-{level}", "reasoning_effort": level}})
    specs._intelligence_classes = load_intelligence_classes(str(tmp_path))
    effective = profile("low")
    assert specs._resolve_model(effective, codex, "fast-high") == "codex-high"
    effective._agent_intelligence_class = "fast-medium"
    assert specs._resolve_model(effective, codex, "fast-high") == "codex-medium"


def test_codex_cli_with_custom_provider_keeps_that_provider_mapping(tmp_path, codex):
    specs = builder(tmp_path)
    custom = {"model": "local-model", "reasoning_effort": "medium"}
    specs._intelligence_classes["fast-low"].mapping["local-provider"] = custom
    harness = SimpleNamespace(**vars(codex), provider="local-provider", env_map=codex.env_map)
    assert specs._resolve_class_config(profile(), harness, None) == custom
    assert specs._resolve_model(profile(), harness, None) == "local-model"
