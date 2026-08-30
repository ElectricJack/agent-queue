"""Intelligence classes — vault-authored (name, description, provider→model+thinking).

Loaded from ``vault/intelligence-classes/<id>.md``. Each file is markdown with
YAML frontmatter (``id``, ``name``, ``description``) followed by a single
fenced ```json``` block mapping provider name to a runtime config slice
(``model``, plus provider-appropriate thinking / reasoning fields).

Resolution: (class_id, provider) → dict. Session launch calls
:func:`resolve_class` after picking the class (from ``task.intelligence_class``
or the profile's ``default_class``) and the provider (from the profile's
harness). An optional ``codex`` slice overrides the OpenAI API slice only
for the Codex CLI.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace

import yaml

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class IntelligenceClass:
    id: str
    name: str
    description: str
    mapping: dict  # provider -> {"model": str, ...}


def _parse_file(path: str) -> IntelligenceClass | None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        logger.warning("intelligence-class %s: no frontmatter", path)
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        logger.warning("intelligence-class %s: bad YAML frontmatter", path)
        return None
    body = parts[2]
    m = _JSON_BLOCK_RE.search(body)
    if not m:
        logger.warning("intelligence-class %s: missing fenced json block", path)
        return None
    try:
        mapping = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("intelligence-class %s: bad JSON — %s", path, exc)
        return None
    if not isinstance(mapping, dict):
        logger.warning("intelligence-class %s: mapping must be an object", path)
        return None
    return IntelligenceClass(
        id=str(fm.get("id") or os.path.splitext(os.path.basename(path))[0]),
        name=str(fm.get("name") or ""),
        description=str(fm.get("description") or ""),
        mapping=mapping,
    )


def _backfill_legacy_fast_codex(cls: IntelligenceClass) -> IntelligenceClass:
    """Add the bundled CLI default only to unchanged legacy API defaults.

    The vault is never rewritten. Explicit Codex entries (even empty ones),
    custom OpenAI slices, other class IDs, and other providers are preserved.
    Compare against the historical API slice, not tomorrow's bundled API model.
    """
    legacy_efforts = {"fast-off": "minimal", "fast-low": "low",
                      "fast-medium": "medium", "fast-high": "high"}
    effort = legacy_efforts.get(cls.id)
    if effort is None or "codex" in cls.mapping:
        return cls
    if cls.mapping.get("openai") != {"model": "gpt-5-mini", "reasoning_effort": effort}:
        return cls
    source = os.path.join(os.path.dirname(__file__), "..", "prompts",
                          "default_intelligence_classes", f"{cls.id}.md")
    try:
        bundled = _parse_file(source)
    except OSError:
        logger.warning("Bundled Codex default for intelligence-class %s is unavailable", cls.id)
        return cls
    codex = resolve_class(bundled, "codex") if bundled else {}
    if not codex.get("model"):
        return cls
    return replace(cls, mapping={**cls.mapping, "codex": codex})


def load_intelligence_classes(data_dir: str) -> dict[str, IntelligenceClass]:
    """Load every ``*.md`` under ``{data_dir}/vault/intelligence-classes/``.

    Returns ``{}`` when the directory does not exist or contains no valid files.
    Silently skips files with parse errors (warnings logged).
    """
    root = os.path.join(data_dir, "vault", "intelligence-classes")
    if not os.path.isdir(root):
        return {}
    out: dict[str, IntelligenceClass] = {}
    for name in sorted(os.listdir(root)):
        if not name.endswith(".md"):
            continue
        cls = _parse_file(os.path.join(root, name))
        if cls is not None:
            out[cls.id] = _backfill_legacy_fast_codex(cls)
    return out


def resolve_class(cls: IntelligenceClass, provider: str) -> dict:
    """Return the config slice for *provider*, or ``{}`` if not defined."""
    slice_ = cls.mapping.get(provider)
    if not isinstance(slice_, dict):
        return {}
    return dict(slice_)
