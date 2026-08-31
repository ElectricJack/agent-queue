from pathlib import Path

from src.intelligence_classes import (
    IntelligenceClass,
    load_intelligence_classes,
    resolve_class,
)
from src.vault import ensure_default_intelligence_classes


# 12-class matrix: 3 capability tiers × 4 thinking levels. Explicit names
# only; no legacy aliases. Every class covers anthropic + openai + google.
TIERS = {"fast", "standard", "deep"}
THINKING = {"off", "low", "medium", "high"}
DEFAULTS = {f"{tier}-{level}" for tier in TIERS for level in THINKING}


def test_defaults_shipped(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    root = Path(tmp_path) / "vault" / "intelligence-classes"
    assert {p.stem for p in root.glob("*.md")} == DEFAULTS


def test_load_parses_frontmatter_and_mapping(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    assert set(classes) == DEFAULTS
    fast = classes["fast-medium"]
    assert isinstance(fast, IntelligenceClass)
    assert fast.mapping["anthropic"]["model"]
    # Every class must cover all three providers.
    for cid, cls in classes.items():
        assert set(cls.mapping.keys()) >= {"anthropic", "openai", "google"}, cid
        for provider in ("anthropic", "openai", "google"):
            assert cls.mapping[provider].get("model"), f"{cid}/{provider} missing model"


def test_resolve_class_returns_provider_slice(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))["standard-medium"]
    slice_ = resolve_class(cls, "anthropic")
    assert "model" in slice_


def test_resolve_class_unknown_provider_returns_empty(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))["standard-medium"]
    assert resolve_class(cls, "unicorn") == {}


def test_missing_dir_returns_empty_dict(tmp_path):
    assert load_intelligence_classes(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# Load error paths and legacy upgrades (plan §intelligence 18-19)
# ---------------------------------------------------------------------------


def _write_class(data_dir, filename: str, body: str) -> Path:
    root = Path(data_dir) / "vault" / "intelligence-classes"
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_load_skips_bad_yaml_json_symlink_and_preserves_valid_neighbor(tmp_path, caplog):
    import logging

    data_dir = tmp_path / "data"

    _write_class(
        data_dir,
        "aa-valid.md",
        "---\nid: aa-valid\nname: Valid\ndescription: ok\n---\n\n"
        '```json\n{"anthropic": {"model": "claude-opus-5"}}\n```\n',
    )
    _write_class(
        data_dir,
        "bb-bad-yaml.md",
        '---\nid: bb-bad-yaml\nbroken: [1, 2\n---\n\n```json\n{"anthropic": {}}\n```\n',
    )
    _write_class(
        data_dir,
        "cc-bad-json.md",
        "---\nid: cc-bad-json\n---\n\n```json\n{not json at all}\n```\n",
    )
    _write_class(
        data_dir,
        "dd-no-json-block.md",
        "---\nid: dd-no-json-block\n---\n\nJust prose, no fenced block.\n",
    )
    _write_class(
        data_dir,
        "ee-mapping-not-object.md",
        '---\nid: ee-mapping-not-object\n---\n\n```json\n["nope"]\n```\n',
    )

    # A symlink pointing outside the vault is never followed, even when the
    # target itself parses cleanly.
    external = tmp_path / "external.md"
    external.write_text(
        '---\nid: ff-external\n---\n\n```json\n{"anthropic": {"model": "x"}}\n```\n',
        encoding="utf-8",
    )
    link = Path(data_dir) / "vault" / "intelligence-classes" / "ff-external.md"
    link.symlink_to(external)

    # A non-.md file is ignored outright.
    (Path(data_dir) / "vault" / "intelligence-classes" / "notes.txt").write_text("x")

    with caplog.at_level(logging.WARNING, logger="src.intelligence_classes"):
        classes = load_intelligence_classes(str(data_dir))

    assert set(classes) == {"aa-valid"}
    assert classes["aa-valid"].mapping == {"anthropic": {"model": "claude-opus-5"}}
    assert classes["aa-valid"].revision  # SHA of the raw bytes is recorded

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bad YAML frontmatter" in m and "bb-bad-yaml" in m for m in messages)
    assert any("bad JSON" in m and "cc-bad-json" in m for m in messages)
    assert any("missing fenced json block" in m and "dd-no-json-block" in m for m in messages)
    assert any("mapping must be an object" in m and "ee-mapping-not-object" in m for m in messages)
    # Skipping the symlink is silent, not a parse warning.
    assert not any("ff-external" in m for m in messages)


def test_legacy_upgrade_is_provider_specific_and_customized_class_is_unchanged(tmp_path):
    data_dir = tmp_path / "data"

    # Historical anthropic slice + a hand-edited openai slice: only the
    # anthropic half may be refreshed, and no codex model may be inferred
    # onto the custom API slice.
    _write_class(
        data_dir,
        "standard-medium.md",
        "---\nid: standard-medium\n---\n\n```json\n"
        '{"anthropic": {"model": "claude-sonnet-5", "thinking": "medium"},\n'
        ' "openai": {"model": "my-own-model", "reasoning_effort": "medium"}}\n```\n',
    )
    # Historical openai slice + a hand-edited anthropic slice: the mirror
    # image, and here codex *is* inferred from the bundled defaults.
    _write_class(
        data_dir,
        "deep-high.md",
        "---\nid: deep-high\n---\n\n```json\n"
        '{"anthropic": {"model": "my-own-claude", "thinking": "high"},\n'
        ' "openai": {"model": "gpt-5", "reasoning_effort": "high"}}\n```\n',
    )
    # Explicitly customized: untouched even though both slices are historical.
    _write_class(
        data_dir,
        "fast-low.md",
        "---\nid: fast-low\ncustomized: true\n---\n\n```json\n"
        '{"anthropic": {"model": "claude-haiku-4-5", "thinking": "low"},\n'
        ' "openai": {"model": "gpt-5-mini", "reasoning_effort": "low"}}\n```\n',
    )
    # Not part of the bundled matrix: never upgraded.
    _write_class(
        data_dir,
        "bespoke-medium.md",
        "---\nid: bespoke-medium\n---\n\n```json\n"
        '{"anthropic": {"model": "claude-sonnet-5", "thinking": "medium"}}\n```\n',
    )

    classes = load_intelligence_classes(str(data_dir))
    bundled = load_intelligence_classes(str(_bundled_data_dir(tmp_path)))

    standard = classes["standard-medium"]
    assert standard.mapping["anthropic"] == bundled["standard-medium"].mapping["anthropic"]
    assert standard.mapping["anthropic"]["model"] != "claude-sonnet-5"
    assert standard.mapping["openai"] == {"model": "my-own-model", "reasoning_effort": "medium"}
    assert "codex" not in standard.mapping

    deep = classes["deep-high"]
    assert deep.mapping["anthropic"] == {"model": "my-own-claude", "thinking": "high"}
    assert deep.mapping["openai"] == bundled["deep-high"].mapping["openai"]
    assert deep.mapping["codex"] == bundled["deep-high"].mapping["codex"]

    fast = classes["fast-low"]
    assert fast.customized is True
    assert fast.mapping == {
        "anthropic": {"model": "claude-haiku-4-5", "thinking": "low"},
        "openai": {"model": "gpt-5-mini", "reasoning_effort": "low"},
    }

    assert classes["bespoke-medium"].mapping == {
        "anthropic": {"model": "claude-sonnet-5", "thinking": "medium"}
    }


def _bundled_data_dir(tmp_path) -> Path:
    """Materialise the shipped defaults in their own data dir for comparison."""
    reference = tmp_path / "reference"
    ensure_default_intelligence_classes(str(reference))
    return reference
