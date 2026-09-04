"""Discover mechanically complete Playbooks V2 artifact fixtures."""

from pathlib import Path
import re
import yaml

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

def review_frontmatter(directory: Path) -> dict | None:
    manifest = directory / "manifest.md"
    if not manifest.is_file():
        return None
    match = _FRONTMATTER.match(manifest.read_text(encoding="utf-8"))
    parsed = yaml.safe_load(match.group(1)) if match else None
    return parsed if isinstance(parsed, dict) else None

def activatable_fixture_ids(fixture_root: Path) -> tuple[str, ...]:
    ready = []
    for directory in sorted(p for p in fixture_root.iterdir() if p.is_dir()):
        manifest = review_frontmatter(directory)
        if manifest is None or manifest.get("playbook_id") != directory.name:
            continue
        if not (directory / "artifact.json").is_file():
            continue
        ready.append(directory.name)
    return tuple(ready)
