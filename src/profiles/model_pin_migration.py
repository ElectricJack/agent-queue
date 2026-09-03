"""One-shot removal of legacy per-profile ``Config.model`` pins.

This runs before profile markdown is parsed at startup, because the parser
intentionally rejects the retired key.  A pin is always removed: matching pins
are expected legacy state, while a mismatch is logged so an operator can audit
the formerly contradictory configuration.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from src.intelligence_classes import load_intelligence_classes, resolve_class

logger = logging.getLogger(__name__)

_CONFIG_BLOCK = re.compile(r"(## Config\s*\n```json\s*\n)(.*?)(\n```)", re.DOTALL)


@dataclass(frozen=True)
class ModelPinMigrationResult:
    path: str
    model: str
    default_class: str
    matched: bool


def _provider_for_harness(harness: object) -> str:
    value = str(harness or "").strip().lower()
    if value == "claude":
        return "anthropic"
    if value == "codex":
        return "codex"
    if value == "gemini":
        return "google"
    return value


def migrate_vault_profile_model_pins(vault_root: str | Path) -> list[ModelPinMigrationResult]:
    """Remove legacy model pins from every vault profile markdown file.

    The migration is idempotent.  It compares a pin to the model selected by
    the profile's class and harness when both can be resolved; unresolved
    classes also lose their pin, with a warning rather than silently preserving
    a configuration the runtime no longer supports.
    """
    root = Path(vault_root)
    classes = load_intelligence_classes(str(root.parent))
    results: list[ModelPinMigrationResult] = []
    for path in root.glob("**/agent-types/**/profile.md"):
        text = path.read_text(encoding="utf-8")
        match = _CONFIG_BLOCK.search(text)
        if not match:
            continue
        try:
            config = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
        if not isinstance(config, dict) or "model" not in config:
            continue
        pin = str(config.pop("model") or "")
        class_id = str(config.get("default_class") or "")
        provider = _provider_for_harness(config.get("harness"))
        resolved = {}
        if class_id and provider and (cls := classes.get(class_id)):
            resolved = resolve_class(cls, provider)
            if provider == "codex" and not resolved:
                resolved = resolve_class(cls, "openai")
        resolved_model = str(resolved.get("model") or "")
        matched = bool(resolved_model and pin == resolved_model)
        if not matched:
            logger.warning(
                "Dropped legacy profile model pin %r from %s; class %r resolves to %r",
                pin, path, class_id, resolved_model or "(unresolved)",
            )
        replacement = match.group(1) + json.dumps(config, indent=2) + match.group(3)
        path.write_text(text[:match.start()] + replacement + text[match.end():], encoding="utf-8")
        results.append(ModelPinMigrationResult(str(path), pin, class_id, matched))
    if results:
        logger.info("Removed legacy model pins from %d profile(s)", len(results))
    return results
