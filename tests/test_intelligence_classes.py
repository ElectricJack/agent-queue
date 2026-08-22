from pathlib import Path

from src.intelligence_classes import (
    IntelligenceClass,
    load_intelligence_classes,
    resolve_class,
)
from src.vault import ensure_default_intelligence_classes


DEFAULTS = {"fast", "standard", "deep"}


def test_defaults_shipped(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    root = Path(tmp_path) / "vault" / "intelligence-classes"
    assert {p.stem for p in root.glob("*.md")} == DEFAULTS


def test_load_parses_frontmatter_and_mapping(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    classes = load_intelligence_classes(str(tmp_path))
    assert set(classes) == DEFAULTS
    fast = classes["fast"]
    assert isinstance(fast, IntelligenceClass)
    assert fast.mapping["anthropic"]["model"]


def test_resolve_class_returns_provider_slice(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))["standard"]
    slice_ = resolve_class(cls, "anthropic")
    assert "model" in slice_


def test_resolve_class_unknown_provider_returns_empty(tmp_path):
    ensure_default_intelligence_classes(str(tmp_path))
    cls = load_intelligence_classes(str(tmp_path))["standard"]
    assert resolve_class(cls, "unicorn") == {}


def test_missing_dir_returns_empty_dict(tmp_path):
    assert load_intelligence_classes(str(tmp_path)) == {}
