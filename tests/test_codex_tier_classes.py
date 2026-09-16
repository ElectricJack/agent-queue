"""Codex tiers launch the requested current model families at the class effort.

The shipped set is seven classes (``src/prompts/default_intelligence_classes``):
``fast-{low,high}``, ``standard-high``, ``deep-{low,high}`` and the OpenAI-only
``astra-{low,high}``.  Every ``-high`` class spends Codex's *extra*-high effort.
"""
import json
from types import SimpleNamespace

import pytest

from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import Harness
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes

#: (class id, Codex model, ``model_reasoning_effort``) for every shipped class
#: with an OpenAI slice above the fast tier.
CLASSES = [
    ("standard-high", "gpt-5.6-terra", "xhigh"),
    ("deep-low", "gpt-5.6-sol", "low"),
    ("deep-high", "gpt-5.6-sol", "xhigh"),
    ("astra-low", "gpt-6-astra", "low"),
    ("astra-high", "gpt-6-astra", "xhigh"),
]

#: The classes that also existed in the retired tier x level matrix, with the
#: historical bundled OpenAI slice a pre-cutover vault still carries.  Astra
#: is new and has no legacy slice to upgrade from.
LEGACY = [
    ("standard-high", "gpt-5.6-terra", "high", "xhigh"),
    ("deep-low", "gpt-5.6-sol", "low", "low"),
    ("deep-high", "gpt-5.6-sol", "high", "xhigh"),
]


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


@pytest.mark.parametrize(("cid", "model", "effort"), CLASSES)
@pytest.mark.parametrize("lifecycle", ["task", "named", "pool"])
def test_current_codex_tiers_reach_launch_and_flock_snapshot(tmp_path, cid, model, effort, lifecycle):
    ensure_default_intelligence_classes(str(tmp_path))
    specs = make_builder(load_intelligence_classes(str(tmp_path)))
    profile = make_profile(cid)
    harness = Harness(id="codex", command="codex", model_flag="-m")
    kwargs = {"profile": profile, "harness": harness, "work_dir": "/wd", "session_id": "s",
              "instance_token": "instance", "prompt": "start"}
    if lifecycle == "named":
        spec = specs.build_named_spec(project_id=None, **kwargs)
    elif lifecycle == "pool":
        spec = specs.build_pool_spec(
            project=SimpleNamespace(id="p", name="Project"), agent_id="a",
            session_name="p-worker--p--test", **kwargs,
        )
    else:
        spec = specs.build_task_spec(task=SimpleNamespace(id="t", project_id="p", intelligence_class=None), **kwargs)
    assert spec.command[spec.command.index("-m") + 1] == model
    assert spec.command[spec.command.index("-c") + 1] == f'model_reasoning_effort="{effort}"'
    assert resolve_launch_settings(profile, harness, specs) == {
        "llm_provider": "openai", "model": model, "intelligence_class": cid,
    }


@pytest.mark.parametrize(("cid", "model", "legacy_effort", "effort"), LEGACY)
def test_legacy_bundled_tiers_upgrade_api_without_touching_vault(tmp_path, cid, model, legacy_effort, effort):
    original = {"openai": {"model": "gpt-5", "reasoning_effort": legacy_effort},
                "anthropic": {"model": "custom-anthropic", "thinking": "custom"}}
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    cls = load_intelligence_classes(str(tmp_path))[cid]
    assert cls.mapping == {**original,
                           "openai": {"model": model, "reasoning_effort": effort},
                           "codex": {"model": model, "reasoning_effort": effort}}
    assert cls.name == "User name" and path.read_bytes() == content


@pytest.mark.parametrize(("cid", "model", "legacy_effort", "effort"), LEGACY)
@pytest.mark.parametrize("case", ["model", "effort", "extra", "codex", "empty_codex"])
def test_custom_tier_slices_are_not_overridden(tmp_path, cid, model, legacy_effort, effort, case):
    mapping = {"openai": {"model": "gpt-5", "reasoning_effort": legacy_effort}}
    if case == "model":
        mapping["openai"]["model"] = "custom-openai"
    elif case == "effort":
        mapping["openai"]["reasoning_effort"] = "medium"
    elif case == "extra":
        mapping["openai"]["custom"] = True
    elif case == "codex":
        mapping["codex"] = {"model": "custom-codex", "reasoning_effort": "high"}
    else:
        mapping["codex"] = {}
    path = write_class(tmp_path, cid, mapping)
    content = path.read_bytes()
    # An explicit Codex entry (even an empty one) never acquires the bundled
    # Codex slice, while the untouched historical API slice still upgrades.
    expected = mapping if case not in {"codex", "empty_codex"} else {
        **mapping, "openai": {"model": model, "reasoning_effort": effort},
    }
    assert load_intelligence_classes(str(tmp_path))[cid].mapping == expected
    assert path.read_bytes() == content


@pytest.mark.parametrize(("cid", "model", "effort"), CLASSES)
def test_current_tiers_preserve_agent_pins_and_other_provider_behavior(tmp_path, cid, model, effort):
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    specs = make_builder(classes)
    profile = make_profile(cid, pin="operator-pin")
    harness = Harness(id="codex", command="codex", model_flag="-m")
    assert specs._resolve_model(profile, harness, None) == "operator-pin"
    profile = make_profile(cid)
    api_harness = SimpleNamespace(id="api", command="api", provider="openai")
    assert specs._resolve_class_config(profile, api_harness, None) == {"model": model, "reasoning_effort": effort}
    custom = SimpleNamespace(id="codex", command="codex", provider="local")
    classes[cid].mapping["local"] = {"model": "local-model"}
    assert specs._resolve_class_config(profile, custom, None) == {"model": "local-model"}


@pytest.mark.parametrize("cid", ["astra-low", "astra-high"])
def test_astra_has_no_model_for_a_non_codex_harness(tmp_path, cid):
    """Astra is OpenAI-only: a Claude harness on it resolves nothing, not a stand-in."""
    ensure_default_intelligence_classes(str(tmp_path))
    specs = make_builder(load_intelligence_classes(str(tmp_path)))
    profile = make_profile(cid)
    claude = SimpleNamespace(id="claude", command="claude", provider="anthropic")
    assert specs._resolve_class_config(profile, claude, None) == {}
