"""Tier policy upgrades only unchanged bundled provider settings in memory.

The shipped set is seven classes (``src/prompts/default_intelligence_classes``):
``fast-{low,high}``, ``standard-high``, ``deep-{low,high}`` and the OpenAI-only
``astra-{low,high}``.  Every ``-high`` class spends the provider's *extra*-high
effort; there is no ``google`` slice from the deep tier upward.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import parse_harness_markdown
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes

#: (class id, tier, level, Claude model, OpenAI model) for every shipped class
#: that has an Anthropic slice.
CLASSES = [
    ("fast-low", "fast", "low", "claude-sonnet-5", "gpt-5.6-luna"),
    ("fast-high", "fast", "high", "claude-sonnet-5", "gpt-5.6-luna"),
    ("standard-high", "standard", "high", "claude-opus-5", "gpt-5.6-terra"),
    ("deep-low", "deep", "low", "claude-fable-5", "gpt-5.6-sol"),
    ("deep-high", "deep", "high", "claude-fable-5", "gpt-5.6-sol"),
]
#: The provider effort each level buys: ``-high`` is the provider's extra-high.
EFFORT = {"low": "low", "high": "xhigh"}
#: The historical bundled models a pre-cutover vault still carries per tier.
LEGACY_CLAUDE = {"fast": "claude-haiku-4-5", "standard": "claude-sonnet-5", "deep": "claude-opus-5"}
LEGACY_OPENAI = {"fast": "gpt-5-mini", "standard": "gpt-5", "deep": "gpt-5"}
#: Rungs the cut to seven classes retired (``src/profiles/class_retirement.py``).
RETIRED = ["fast-off", "fast-medium", "standard-off", "standard-low", "standard-medium",
           "deep-off", "deep-medium"]


def write_class(tmp_path, cid, mapping):
    directory = tmp_path / "vault" / "intelligence-classes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cid}.md"
    path.write_text(f'---\nid: {cid}\nname: Preserved name\n---\nUser prose.\n```json\n' + json.dumps(mapping) + '\n```\n')
    return path


def current_slices(claude, openai, level):
    effort = EFFORT[level]
    return {
        "anthropic": {"model": claude, "thinking": effort},
        "openai": {"model": openai, "reasoning_effort": effort},
        "codex": {"model": openai, "reasoning_effort": effort},
    }


def legacy_slices(tier, level):
    return {
        "anthropic": {"model": LEGACY_CLAUDE[tier], "thinking": level},
        "openai": {"model": LEGACY_OPENAI[tier], "reasoning_effort": level},
    }


@pytest.mark.parametrize(("cid", "tier", "level", "claude", "openai"), CLASSES)
def test_all_bundled_provider_models_and_efforts(tmp_path, cid, tier, level, claude, openai):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))[cid]
    expected = current_slices(claude, openai, level)
    assert {key: cls.mapping[key] for key in expected} == expected
    if tier == "deep":
        assert "google" not in cls.mapping
    else:
        assert cls.mapping["google"]["model"] == "gemini-2.5-flash"


@pytest.mark.parametrize("level", ["low", "high"])
def test_astra_is_openai_only(tmp_path, level):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))[f"astra-{level}"]
    assert cls.mapping == {
        "openai": {"model": "gpt-6-astra", "reasoning_effort": EFFORT[level]},
        "codex": {"model": "gpt-6-astra", "reasoning_effort": EFFORT[level]},
    }


def test_shipped_classes_never_default_google_to_pro(tmp_path):
    """A Gemini slice on the user's Google API key must be Flash, never Pro."""
    ensure_default_intelligence_classes(str(tmp_path))
    for cid, cls in load_intelligence_classes(str(tmp_path)).items():
        google = cls.mapping.get("google")
        if google is None:
            continue
        assert "pro" not in google["model"].lower(), f"{cid} google slice: {google['model']}"


@pytest.mark.parametrize(("cid", "tier", "level", "claude", "openai"), CLASSES)
def test_legacy_provider_slices_upgrade_independently_without_vault_writes(tmp_path, cid, tier, level, claude, openai):
    original = {**legacy_slices(tier, level), "google": {"model": "user-google", "thinking_budget": 99}}
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    cls = load_intelligence_classes(str(tmp_path))[cid]
    assert cls.mapping == {**original, **current_slices(claude, openai, level)}
    assert cls.name == "Preserved name" and path.read_bytes() == content
    assert load_intelligence_classes(str(tmp_path))[cid] == cls


@pytest.mark.parametrize("cid", RETIRED)
def test_retired_class_ids_have_no_bundled_upgrade(tmp_path, cid):
    """A retired rung loads verbatim: repointing it is class retirement's job."""
    tier, _, level = cid.rpartition("-")
    original = legacy_slices(tier, level)
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    assert load_intelligence_classes(str(tmp_path))[cid].mapping == original
    assert path.read_bytes() == content


@pytest.mark.parametrize(("cid", "tier", "level", "claude", "openai"), CLASSES)
@pytest.mark.parametrize("custom_provider", ["anthropic", "openai"])
@pytest.mark.parametrize("edit", ["model", "effort", "extra"])
def test_custom_slice_does_not_block_other_provider_upgrade(tmp_path, cid, tier, level, claude, openai, custom_provider, edit):
    original = legacy_slices(tier, level)
    if edit == "model":
        original[custom_provider]["model"] = "user-model"
    elif edit == "effort":
        original[custom_provider]["thinking" if custom_provider == "anthropic" else "reasoning_effort"] = "medium"
    else:
        original[custom_provider]["custom"] = True
    path = write_class(tmp_path, cid, original)
    content = path.read_bytes()
    mapping = load_intelligence_classes(str(tmp_path))[cid].mapping
    assert mapping[custom_provider] == original[custom_provider]
    other = "openai" if custom_provider == "anthropic" else "anthropic"
    assert mapping[other] == current_slices(claude, openai, level)[other]
    if custom_provider == "openai":
        assert "codex" not in mapping
    else:
        assert mapping["codex"] == {"model": openai, "reasoning_effort": EFFORT[level]}
    assert path.read_bytes() == content


@pytest.mark.parametrize("codex", [{"model": "pinned-codex", "reasoning_effort": "high"}, {}, None])
def test_explicit_codex_survives_independent_provider_upgrade(tmp_path, codex):
    original = {**legacy_slices("deep", "low"), "codex": codex}
    path = write_class(tmp_path, "deep-low", original)
    content = path.read_bytes()
    mapping = load_intelligence_classes(str(tmp_path))["deep-low"].mapping
    expected = current_slices("claude-fable-5", "gpt-5.6-sol", "low")
    assert mapping == {**expected, "codex": codex}
    assert path.read_bytes() == content


def test_user_class_with_old_model_ids_is_not_migrated(tmp_path):
    original = legacy_slices("deep", "low")
    write_class(tmp_path, "custom-deep-low", original)
    assert load_intelligence_classes(str(tmp_path))["custom-deep-low"].mapping == original


@pytest.mark.parametrize(("cid", "effort"), [("deep-low", "low"), ("deep-high", "xhigh")])
@pytest.mark.parametrize("lifecycle", ["task", "named", "pool"])
def test_deep_fable_launch_carries_the_class_effort(tmp_path, monkeypatch, cid, effort, lifecycle):
    """A deep class launches Fable at its own effort and never disables thinking."""
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    builder = SessionSpecBuilder(SimpleNamespace(security=None), intelligence_classes=classes)
    source = Path(__file__).parents[1] / "src/sessions/default_harnesses/claude.md"
    harness = parse_harness_markdown(source.read_text()).harness
    kwargs = {"profile": SimpleNamespace(id="worker", model="fallback", default_class=cid),
              "harness": harness, "work_dir": "/wd", "session_id": "s", "instance_token": "i",
              "prompt": "start"}
    if lifecycle == "named":
        spec = builder.build_named_spec(project_id=None, **kwargs)
    elif lifecycle == "pool":
        spec = builder.build_pool_spec(
            project=SimpleNamespace(id="p", name="Project"), agent_id="a",
            session_name="p-worker--p--test", **kwargs,
        )
    else:
        spec = builder.build_task_spec(task=SimpleNamespace(id="t", project_id="p", intelligence_class=None), **kwargs)
    assert spec.command[spec.command.index("--model") + 1] == "claude-fable-5"
    assert spec.command[spec.command.index("--effort") + 1] == effort
    assert spec.env["CLAUDE_CODE_EFFORT_LEVEL"] == effort
    assert "MAX_THINKING_TOKENS" not in spec.env
