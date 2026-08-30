"""Standard/deep Codex tiers use the requested current model families."""
import json
from types import SimpleNamespace

import pytest

from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import Harness
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes

TIERS = [("standard", "gpt-5.6-terra"), ("deep", "gpt-5.6-sol")]
LEVELS = [("off", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")]


def make_profile(cid, pin=None):
    return apply_agent_overrides(
        SimpleNamespace(id="worker", harness="claude", model="previous-vendor-model", default_class=cid),
        SimpleNamespace(harness="codex", model=pin, intelligence_class=None),
    )


def make_builder(classes):
    return SessionSpecBuilder(SimpleNamespace(security=None), intelligence_classes=classes)


def write_class(tmp_path, cid, mapping):
    directory = tmp_path / "vault" / "intelligence-classes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cid}.md"
    path.write_text(f'---\nid: {cid}\nname: User name\n---\n```json\n' + json.dumps(mapping) + '\n```\n')
    return path


@pytest.mark.parametrize(("tier", "model"), TIERS)
@pytest.mark.parametrize(("level", "effort"), LEVELS)
@pytest.mark.parametrize("lifecycle", ["task", "named", "pool"])
def test_current_codex_tiers_reach_launch_and_flock_snapshot(tmp_path, tier, model, level, effort, lifecycle):
    ensure_default_intelligence_classes(str(tmp_path))
    specs = make_builder(load_intelligence_classes(str(tmp_path)))
    profile = make_profile(f"{tier}-{level}")
    harness = Harness(id="codex", command="codex", model_flag="-m")
    kwargs = dict(profile=profile, harness=harness, work_dir="/wd", session_id="s",
                  instance_token="instance", prompt="start")
    if lifecycle == "named":
        spec = specs.build_named_spec(project_id=None, **kwargs)
    elif lifecycle == "pool":
        spec = specs.build_pool_spec(project=SimpleNamespace(id="p", name="Project"), agent_id="a", **kwargs)
    else:
        spec = specs.build_task_spec(task=SimpleNamespace(id="t", project_id="p", intelligence_class=None), **kwargs)
    assert spec.command[spec.command.index("-m") + 1] == model
    assert spec.command[spec.command.index("-c") + 1] == f'model_reasoning_effort="{effort}"'
    assert resolve_launch_settings(profile, harness, specs) == {
        "llm_provider": "openai", "model": model, "intelligence_class": f"{tier}-{level}",
    }


@pytest.mark.parametrize(("tier", "model"), TIERS)
@pytest.mark.parametrize(("level", "effort"), LEVELS)
def test_legacy_bundled_tiers_upgrade_api_without_touching_vault(tmp_path, tier, model, level, effort):
    cid = f"{tier}-{level}"
    original = {"openai": {"model": "gpt-5", "reasoning_effort": "minimal" if level == "off" else level},
                "anthropic": {"model": "custom-anthropic", "thinking": "custom"}}
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    cls = load_intelligence_classes(str(tmp_path))[cid]
    assert cls.mapping == {**original,
                           "openai": {"model": model, "reasoning_effort": "none" if level == "off" else level},
                           "codex": {"model": model, "reasoning_effort": effort}}
    assert cls.name == "User name" and path.read_bytes() == content


@pytest.mark.parametrize(("tier", "model"), TIERS)
@pytest.mark.parametrize("case", ["model", "effort", "extra", "codex", "empty_codex"])
def test_custom_tier_slices_are_not_overridden(tmp_path, tier, model, case):
    mapping = {"openai": {"model": "gpt-5", "reasoning_effort": "low"}}
    if case == "model":
        mapping["openai"]["model"] = "custom-openai"
    elif case == "effort":
        mapping["openai"]["reasoning_effort"] = "high"
    elif case == "extra":
        mapping["openai"]["custom"] = True
    elif case == "codex":
        mapping["codex"] = {"model": "custom-codex", "reasoning_effort": "high"}
    else:
        mapping["codex"] = {}
    path = write_class(tmp_path, f"{tier}-low", mapping)
    content = path.read_bytes()
    expected = mapping if case not in {"codex", "empty_codex"} else {
        **mapping, "openai": {"model": model, "reasoning_effort": "low"},
    }
    assert load_intelligence_classes(str(tmp_path))[f"{tier}-low"].mapping == expected
    assert path.read_bytes() == content


@pytest.mark.parametrize(("tier", "model"), TIERS)
def test_current_tiers_preserve_agent_pins_and_other_provider_behavior(tmp_path, tier, model):
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    specs = make_builder(classes)
    profile = make_profile(f"{tier}-low", pin="operator-pin")
    harness = Harness(id="codex", command="codex", model_flag="-m")
    assert specs._resolve_model(profile, harness, None) == "operator-pin"
    profile = make_profile(f"{tier}-low")
    api_harness = SimpleNamespace(id="api", command="api", provider="openai")
    assert specs._resolve_class_config(profile, api_harness, None) == {"model": model, "reasoning_effort": "low"}
    custom = SimpleNamespace(id="codex", command="codex", provider="local")
    classes[f"{tier}-low"].mapping["local"] = {"model": "local-model"}
    assert specs._resolve_class_config(profile, custom, None) == {"model": "local-model"}
