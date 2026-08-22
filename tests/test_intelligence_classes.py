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
DEFAULTS = {f"{t}-{l}" for t in TIERS for l in THINKING}


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
