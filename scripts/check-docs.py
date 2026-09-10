#!/usr/bin/env python3
"""Check GitHub Markdown links, anchors, and module-catalog coverage locally.

The checker is deliberately dependency-free.  Check just the pages you edited
while writing; ``--all`` is available to the documentation acceptance owner.
Module coverage uses the ownership manifest rather than a second handwritten
list, so a newly classified production module must have a linked catalog row.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/plans/documentation-overhaul/module-ownership.json"
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", re.MULTILINE | re.DOTALL)
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "data", "javascript"}


def github_anchor(value: str) -> str:
    """Return GitHub's stable heading slug for ordinary Markdown headings."""
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_~]", "", value).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"[ ]+", "-", value)


def anchors(path: Path) -> set[str]:
    """Return all GitHub heading anchors in *path*, including duplicate suffixes."""
    text = FENCE_RE.sub("", path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    result: set[str] = set()
    for match in HEADING_RE.finditer(text):
        slug = github_anchor(match.group(1))
        if not slug:
            continue
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def markdown_links(path: Path) -> Iterable[str]:
    """Yield local link destinations, excluding examples inside fenced code."""
    text = FENCE_RE.sub("", path.read_text(encoding="utf-8"))
    for match in LINK_RE.finditer(text):
        yield match.group(1).strip().split(maxsplit=1)[0].strip("<>")
    for match in REFERENCE_LINK_RE.finditer(text):
        yield match.group(1).strip("<>")


def resolve_target(source: Path, destination: str) -> tuple[Path | None, str | None]:
    """Resolve an internal Markdown link, returning ``(file, decoded-anchor)``."""
    parsed = urlsplit(destination)
    if parsed.scheme.lower() in EXTERNAL_SCHEMES or parsed.netloc:
        return None, None
    target = unquote(parsed.path)
    anchor = unquote(parsed.fragment) or None
    if not target:
        return source, anchor
    return (source.parent / target).resolve(), anchor


def check_links(paths: Iterable[Path]) -> list[str]:
    """Return readable failures for links and anchors in the selected pages."""
    problems: list[str] = []
    anchors_by_path: dict[Path, set[str]] = {}
    for source in paths:
        for destination in markdown_links(source):
            target, fragment = resolve_target(source, destination)
            if target is None:
                continue
            if not target.is_file():
                problems.append(f"{source.relative_to(ROOT)}: {destination}: target does not exist")
                continue
            if fragment:
                target_anchors = anchors_by_path.setdefault(target, anchors(target))
                if fragment not in target_anchors:
                    problems.append(
                        f"{source.relative_to(ROOT)}: {destination}: anchor #{fragment} does not exist",
                    )
    return problems


def catalog_links(catalog: Path) -> set[Path]:
    """Return existing repository files named by Markdown links in a catalog."""
    found: set[Path] = set()
    for destination in markdown_links(catalog):
        target, _fragment = resolve_target(catalog, destination)
        if target is not None and target.is_file():
            found.add(target.relative_to(ROOT))
    return found


def check_module_coverage(manifest_path: Path, shard: str) -> list[str]:
    """Check catalog rows for one manifest shard, or all shards when requested."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    modules = manifest.get("modules", {})
    shards = manifest.get("shards", {})
    requested = set(shards) if shard == "all" else {shard}
    unknown = sorted(requested - set(shards))
    if unknown:
        return [f"unknown module shard(s): {', '.join(unknown)}"]

    problems: list[str] = []
    catalog_cache: dict[str, set[Path]] = {}
    for source, entry in sorted(modules.items()):
        category = entry.get("category")
        owner = entry.get("shard")
        purpose = entry.get("purpose")
        if not purpose:
            problems.append(f"{source}: intentional exclusion/category has no reason")
        if owner not in requested or category != "production":
            continue
        catalog_value = entry.get("catalog")
        if not catalog_value:
            problems.append(f"{source}: production module has no catalog for shard {owner}")
            continue
        catalog = (ROOT / str(catalog_value)).resolve()
        if not catalog.is_file():
            problems.append(f"{source}: catalog does not exist: {catalog_value}")
            continue
        links = catalog_cache.setdefault(str(catalog), catalog_links(catalog))
        if Path(source) not in links:
            problems.append(f"{source}: missing linked row in {catalog_value}")
    return problems


def selected_markdown(paths: list[str], include_all: bool) -> list[Path]:
    """Expand selected files/directories without silently checking the whole tree."""
    values = paths or (["docs", "README.md"] if include_all else [])
    selected: list[Path] = []
    for value in values:
        path = (ROOT / value).resolve()
        if path.is_file() and path.suffix == ".md":
            selected.append(path)
        elif path.is_dir():
            selected.extend(sorted(path.rglob("*.md")))
        else:
            raise ValueError(f"not a Markdown file or directory: {value}")
    return sorted(set(selected))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown files or directories to check")
    parser.add_argument("--all", action="store_true", help="check docs/ and README.md")
    parser.add_argument(
        "--module-coverage", metavar="SHARD", help="check one catalog shard, or 'all'",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    problems: list[str] = []
    try:
        pages = selected_markdown(args.paths, args.all)
    except ValueError as error:
        parser.error(str(error))
    if not pages and not args.module_coverage:
        parser.error("provide a Markdown path, --all, or --module-coverage")
    problems.extend(check_links(pages))
    if args.module_coverage:
        problems.extend(check_module_coverage(args.manifest.resolve(), args.module_coverage))

    if problems:
        print("documentation check failed:")
        print("\n".join(f"  - {problem}" for problem in problems))
        return 1
    labels: list[str] = []
    if pages:
        labels.append(f"{len(pages)} Markdown file(s)")
    if args.module_coverage:
        labels.append(f"module shard {args.module_coverage}")
    print("documentation check passed: " + ", ".join(labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
