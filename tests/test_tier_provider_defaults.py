"""Tier policy upgrades only unchanged bundled provider settings in memory."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import parse_harness_markdown
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes

TIERS = [
    ("fast", "claude-haiku-4-5", "claude-sonnet-5", "gpt-5-mini", "gpt-5.6-luna"),
    ("standard", "claude-sonnet-5", "claude-opus-5", "gpt-5", "gpt-5.6-terra"),
    ("deep", "claude-opus-5", "claude-fable-5", "gpt-5", "gpt-5.6-sol"),
]
LEVELS = ["off", "low", "medium", "high"]


def write_class(tmp_path, cid, mapping):
    directory = tmp_path / "vault" / "intelligence-classes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cid}.md"
    path.write_text(f'---\nid: {cid}\nname: Preserved name\n---\nUser prose.\n```json\n' + json.dumps(mapping) + '\n```\n')
    return path


def current_slices(tier, claude, openai, level):
    return {
        "anthropic": {"model": claude, "thinking": "low" if tier == "deep" and level == "off" else level},
        "openai": {"model": openai, "reasoning_effort": "none" if level == "off" else level},
        "codex": {"model": openai, "reasoning_effort": "low" if level == "off" else level},
    }


@pytest.mark.parametrize(("tier", "old_claude", "claude", "old_api", "openai"), TIERS)
@pytest.mark.parametrize("level", LEVELS)
def test_all_bundled_provider_models_and_efforts(tmp_path, tier, old_claude, claude, old_api, openai, level):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))[f"{tier}-{level}"]
    expected = current_slices(tier, claude, openai, level)
    assert {key: cls.mapping[key] for key in expected} == expected
    assert cls.mapping["google"]["model"] == ("gemini-2.5-flash" if tier == "fast" else "gemini-2.5-pro")


@pytest.mark.parametrize(("tier", "old_claude", "claude", "old_api", "openai"), TIERS)
@pytest.mark.parametrize("level", LEVELS)
def test_legacy_provider_slices_upgrade_independently_without_vault_writes(tmp_path, tier, old_claude, claude, old_api, openai, level):
    cid = f"{tier}-{level}"
    original = {
        "anthropic": {"model": old_claude, "thinking": level},
        "openai": {"model": old_api, "reasoning_effort": "minimal" if level == "off" else level},
        "google": {"model": "user-google", "thinking_budget": 99},
    }
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    cls = load_intelligence_classes(str(tmp_path))[cid]
    assert cls.mapping == {**original, **current_slices(tier, claude, openai, level)}
    assert cls.name == "Preserved name" and path.read_bytes() == content
    assert load_intelligence_classes(str(tmp_path))[cid] == cls


@pytest.mark.parametrize(("tier", "old_claude", "claude", "old_api", "openai"), TIERS)
@pytest.mark.parametrize("custom_provider", ["anthropic", "openai"])
@pytest.mark.parametrize("edit", ["model", "effort", "extra"])
def test_custom_slice_does_not_block_other_provider_upgrade(tmp_path, tier, old_claude, claude, old_api, openai, custom_provider, edit):
    original = {"anthropic": {"model": old_claude, "thinking": "low"},
                "openai": {"model": old_api, "reasoning_effort": "low"}}
    if edit == "model":
        original[custom_provider]["model"] = "user-model"
    elif edit == "effort":
        original[custom_provider]["thinking" if custom_provider == "anthropic" else "reasoning_effort"] = "high"
    else:
        original[custom_provider]["custom"] = True
    path = write_class(tmp_path, f"{tier}-low", original)
    content = path.read_bytes()
    mapping = load_intelligence_classes(str(tmp_path))[f"{tier}-low"].mapping
    assert mapping[custom_provider] == original[custom_provider]
    other = "openai" if custom_provider == "anthropic" else "anthropic"
    assert mapping[other] == current_slices(tier, claude, openai, "low")[other]
    if custom_provider == "openai":
        assert "codex" not in mapping
    else:
        assert mapping["codex"] == {"model": openai, "reasoning_effort": "low"}
    assert path.read_bytes() == content


@pytest.mark.parametrize("codex", [{"model": "pinned-codex", "reasoning_effort": "high"}, {}, None])
def test_explicit_codex_survives_independent_provider_upgrade(tmp_path, codex):
    original = {"anthropic": {"model": "claude-opus-5", "thinking": "off"},
                "openai": {"model": "gpt-5", "reasoning_effort": "minimal"}, "codex": codex}
    path = write_class(tmp_path, "deep-off", original)
    content = path.read_bytes()
    mapping = load_intelligence_classes(str(tmp_path))["deep-off"].mapping
    expected = current_slices("deep", "claude-fable-5", "gpt-5.6-sol", "off")
    assert mapping == {**expected, "codex": codex}
    assert path.read_bytes() == content


def test_user_class_with_old_model_ids_is_not_migrated(tmp_path):
    original = {"anthropic": {"model": "claude-opus-5", "thinking": "off"},
                "openai": {"model": "gpt-5", "reasoning_effort": "minimal"}}
    write_class(tmp_path, "custom-deep-off", original)
    assert load_intelligence_classes(str(tmp_path))["custom-deep-off"].mapping == original


@pytest.mark.parametrize("lifecycle", ["task", "named", "pool"])
def test_deep_off_fable_never_disables_thinking_in_generated_launch(tmp_path, monkeypatch, lifecycle):
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    builder = SessionSpecBuilder(SimpleNamespace(security=None), intelligence_classes=classes)
    source = Path(__file__).parents[1] / "src/sessions/default_harnesses/claude.md"
    harness = parse_harness_markdown(source.read_text()).harness
    kwargs = dict(profile=SimpleNamespace(id="worker", model="fallback", default_class="deep-off"),
                  harness=harness, work_dir="/wd", session_id="s", instance_token="i", prompt="start")
    if lifecycle == "named":
        spec = builder.build_named_spec(project_id=None, **kwargs)
    elif lifecycle == "pool":
        spec = builder.build_pool_spec(project=SimpleNamespace(id="p", name="Project"), agent_id="a", **kwargs)
    else:
        spec = builder.build_task_spec(task=SimpleNamespace(id="t", project_id="p", intelligence_class=None), **kwargs)
    assert spec.command[spec.command.index("--model") + 1] == "claude-fable-5"
    assert spec.command[spec.command.index("--effort") + 1] == "low"
    assert spec.env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert "MAX_THINKING_TOKENS" not in spec.env

