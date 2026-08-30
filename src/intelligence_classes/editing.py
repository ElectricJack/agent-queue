"""Validated, revision-checked edits to existing intelligence-class Markdown."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from . import IntelligenceClass, _FRONTMATTER_RE, _JSON_BLOCK_RE, _parse_file, _parse_text

_WRITE_LOCK = threading.Lock()
_MISSING = object()
_EFFORTS = {
    "anthropic": ("thinking", {"off", "low", "medium", "high", "xhigh", "max"}),
    "openai": ("reasoning_effort", {"none", "minimal", "low", "medium", "high", "xhigh"}),
    "codex": ("reasoning_effort", {"none", "minimal", "low", "medium", "high", "xhigh"}),
}


class IntelligenceClassEditError(ValueError):
    pass


class IntelligenceClassConflict(IntelligenceClassEditError):
    def __init__(self, revision: str):
        super().__init__("Intelligence class changed since it was loaded. Reload it before saving.")
        self.current_revision = revision


def class_row(cls: IntelligenceClass) -> dict:
    return {
        "id": cls.id,
        "name": cls.name,
        "description": cls.description,
        "mapping": cls.mapping,
        "revision": cls.revision,
    }


def _string(value, label: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise IntelligenceClassEditError(f"{label} must be a string of at most {limit} characters")
    if any(ord(c) < 32 and (not multiline or c not in "\n\r\t") for c in value) or "\x7f" in value:
        raise IntelligenceClassEditError(f"{label} contains invalid control characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise IntelligenceClassEditError(f"{label} contains invalid Unicode") from None
    return value if multiline else value.strip()


def _json_value(value, depth=0) -> None:
    if depth > 20:
        raise IntelligenceClassEditError("mapping is nested too deeply")
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _json_value(child, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise IntelligenceClassEditError("mapping must contain only finite JSON values")


def _same_json(left, right) -> bool:
    """JSON booleans are distinct from numbers, unlike Python equality."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return left == right


def _validate_mapping(mapping, original: dict) -> str:
    if not isinstance(mapping, dict):
        raise IntelligenceClassEditError("mapping must be a JSON object")
    _json_value(mapping)
    try:
        rendered = json.dumps(mapping, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        size = len(rendered.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise IntelligenceClassEditError("mapping must contain valid JSON values") from None
    if size > 65536:
        raise IntelligenceClassEditError("mapping must be at most 64 KiB")
    for provider, config in mapping.items():
        previous = original.get(provider, _MISSING)
        # Preserve unknown legacy values on unrelated edits. New provider slices
        # are objects or null; unknown options inside an object remain untouched.
        if _same_json(config, previous) or config is None or config == {}:
            continue
        if not provider.strip() or not isinstance(config, dict):
            raise IntelligenceClassEditError("Each provider mapping must be an object or null")
        old = previous if isinstance(previous, dict) else {}
        model = config.get("model", _MISSING)
        # Omitting model removes the override and lets launch use its profile
        # fallback. Keep other options intact for later edits.
        if model is not _MISSING and not _same_json(model, old.get("model", _MISSING)):
            checked_model = _string(model, f"{provider}.model", 200)
            if not checked_model:
                raise IntelligenceClassEditError(f"{provider}.model must not be empty")
            if checked_model != model:
                raise IntelligenceClassEditError(
                    f"{provider}.model must not have surrounding whitespace"
                )
        if provider in _EFFORTS:
            field, values = _EFFORTS[provider]
            value = config.get(field, _MISSING)
            if value is not _MISSING and not _same_json(value, old.get(field, _MISSING)):
                if not isinstance(value, str) or value not in values:
                    raise IntelligenceClassEditError(
                        f"{provider}.{field} must be one of {', '.join(sorted(values))}"
                    )
        # Fable supports adaptive thinking only. Preserve an existing legacy
        # pair on unrelated edits, but never introduce a disabled-thinking pair.
        if (
            provider == "anthropic"
            and isinstance(model, str)
            and model.strip().startswith("claude-fable")
            and config.get("thinking") == "off"
            and (not _same_json(model, old.get("model", _MISSING)) or old.get("thinking") != "off")
        ):
            raise IntelligenceClassEditError(
                "Fable requires adaptive thinking; choose low or higher instead of off"
            )
        if provider == "google" and "thinking_budget" in config:
            budget = config["thinking_budget"]
            if not _same_json(budget, old.get("thinking_budget", _MISSING)) and (
                type(budget) is not int or budget < 0
            ):
                raise IntelligenceClassEditError(
                    "google.thinking_budget must be a non-negative integer"
                )
    return rendered


def _find_existing(root: Path, class_id: str) -> Path:
    matches = []
    for path in sorted(root.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(root)
            cls = _parse_file(str(path))
        except (OSError, UnicodeError, ValueError):
            continue
        if cls is not None and cls.id == class_id:
            matches.append(path)
    if len(matches) != 1:
        raise IntelligenceClassEditError("Intelligence class must identify one existing vault file")
    return matches[0]


def edit_intelligence_class(
    data_dir: str,
    *,
    class_id,
    name,
    description,
    mapping,
    expected_revision=None,
) -> IntelligenceClass:
    if not isinstance(class_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", class_id
    ):
        raise IntelligenceClassEditError("class_id must be an existing class identifier")
    name = _string(name, "name", 200)
    if not name:
        raise IntelligenceClassEditError("name must not be empty")
    description = _string(description, "description", 4000, multiline=True)
    if expected_revision is not None and (
        not isinstance(expected_revision, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
    ):
        raise IntelligenceClassEditError(
            "expected_revision must be the class revision returned by the server"
        )
    data_root = Path(data_dir).resolve()
    try:
        root = (data_root / "vault" / "intelligence-classes").resolve(strict=True)
        root.relative_to(data_root)
    except (OSError, ValueError):
        raise IntelligenceClassEditError("Intelligence-class vault is unavailable") from None

    # Atomic replacement alone does not serialize two revision checks. This
    # lock covers all in-process writers; compare raw bytes again before swap to
    # notice a concurrent editor that does not participate in our lock.
    with _WRITE_LOCK:
        path = _find_existing(root, class_id)
        raw = path.read_bytes()
        revision = hashlib.sha256(raw).hexdigest()
        if expected_revision is not None and expected_revision != revision:
            raise IntelligenceClassConflict(revision)
        text = raw.decode("utf-8")
        current = _parse_text(text, str(path), revision=revision)
        if current is None or current.id != class_id:
            raise IntelligenceClassEditError("Intelligence-class source changed or is invalid")
        replacement = _validate_mapping(mapping, current.mapping)
        frontmatter = _FRONTMATTER_RE.match(text)
        body = text[frontmatter.end() :]
        block = _JSON_BLOCK_RE.search(body)
        newline = "\r\n" if "\r\n" in text else "\n"
        rt = YAML(typ="rt")
        rt.preserve_quotes = True
        rt.width = 4096
        rt.line_break = newline
        try:
            metadata = rt.load(frontmatter.group(1))
            metadata["name"] = name
            metadata["description"] = description
            metadata["customized"] = True
            output = io.StringIO()
            rt.dump(metadata, output)
        except (YAMLError, TypeError, ValueError):
            raise IntelligenceClassEditError(
                "Intelligence-class frontmatter cannot be safely edited"
            ) from None
        body = body[: block.start(1)] + replacement.replace("\n", newline) + body[block.end(1) :]
        updated = f"---{newline}{output.getvalue()}---{newline}{body}".encode("utf-8")
        saved = _parse_text(
            updated.decode("utf-8"), str(path), revision=hashlib.sha256(updated).hexdigest()
        )
        if (
            saved is None
            or saved.id != class_id
            or saved.mapping != mapping
            or not saved.customized
        ):
            raise IntelligenceClassEditError(
                "Intelligence-class edit could not be represented safely"
            )
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "wb") as file:
                if hasattr(os, "fchmod"):
                    os.fchmod(file.fileno(), stat.S_IMODE(path.stat().st_mode))
                file.write(updated)
                file.flush()
                os.fsync(file.fileno())
            if path.is_symlink() or path.resolve(strict=True).parent != root:
                raise IntelligenceClassEditError("Intelligence-class source is outside the vault")
            latest = path.read_bytes()
            if latest != raw:
                raise IntelligenceClassConflict(hashlib.sha256(latest).hexdigest())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return saved
