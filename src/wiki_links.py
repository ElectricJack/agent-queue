"""Wiki-link parser, resolver, and content annotation utilities.

Provides tools for working with Obsidian-style ``[[wiki-links]]`` in vault
markdown files.  Used by the hub generator, glossary system, and memory
service to create and follow links in the knowledge graph.

Link format:
    ``[[target]]``              — simple link
    ``[[target|Display Text]]`` — link with display text

Target paths are relative to the vault root, without ``.md`` extension::

    [[projects/agent-queue/memory/insights/_index|Insights]]
    [[glossary/reflection-engine|reflection engine]]
"""

from __future__ import annotations

import re
from pathlib import Path

# Matches [[target]] and [[target|display]]
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def parse_wiki_links(content: str) -> list[dict[str, str]]:
    """Extract wiki-links from markdown content.

    Returns a list of dicts with ``target`` (path) and ``display`` (text)
    keys.  If no display text is provided, ``display`` defaults to the
    last path segment of the target.

    Example::

        >>> parse_wiki_links("See [[glossary/foo|Foo]] and [[bar]]")
        [{'target': 'glossary/foo', 'display': 'Foo'},
         {'target': 'bar', 'display': 'bar'}]
    """
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in WIKI_LINK_RE.finditer(content):
        target = m.group(1).strip()
        display = (m.group(2) or "").strip()
        if not display:
            # Use last path segment as display text
            display = target.rsplit("/", 1)[-1]
        if target not in seen:
            links.append({"target": target, "display": display})
            seen.add(target)
    return links


def resolve_wiki_link(vault_root: str | Path, target: str) -> Path | None:
    """Resolve a wiki-link target to an absolute vault file path.

    Resolution order:

    1. Exact path (``vault_root / target``)
    2. Path with ``.md`` appended (``vault_root / target.md``)
    3. Filename search — find any ``.md`` file matching the last segment

    Returns ``None`` if the target cannot be resolved.
    """
    root = Path(vault_root)
    target = target.strip()

    # 1. Exact path
    candidate = root / target
    if candidate.is_file():
        return candidate

    # 2. With .md extension
    candidate_md = root / f"{target}.md"
    if candidate_md.is_file():
        return candidate_md

    # 3. Filename search (last segment only)
    filename = target.rsplit("/", 1)[-1]
    if not filename:
        return None
    fname_md = f"{filename}.md"
    for p in root.rglob(fname_md):
        if p.is_file():
            return p

    return None


def vault_path_to_wiki_link(vault_root: str | Path, path: str | Path) -> str | None:
    """Convert an absolute or relative vault path to a wiki-link target.

    Strips the vault root prefix and the ``.md`` extension::

        >>> vault_path_to_wiki_link("/vault", "/vault/projects/foo/bar.md")
        'projects/foo/bar'
    """
    try:
        p = Path(path)
        root = Path(vault_root)
        if p.is_absolute():
            rel = p.relative_to(root)
        else:
            rel = p
        # Strip .md extension
        return str(rel.with_suffix(""))
    except (ValueError, TypeError):
        return None


def add_see_also(content: str, links: list[tuple[str, str]]) -> str:
    """Append a ``## See Also`` section to markdown content.

    Parameters
    ----------
    content:
        Existing markdown content.
    links:
        List of ``(target_path, display_text)`` tuples.

    Returns the content with the section appended, or unchanged if
    a ``## See Also`` section already exists or *links* is empty.
    """
    if not links:
        return content
    if "\n## See Also" in content or content.startswith("## See Also"):
        return content

    lines = ["\n\n## See Also"]
    for target, display in links:
        lines.append(f"- [[{target}|{display}]]")
    return content + "\n".join(lines) + "\n"


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content.

    Strips the leading ``---`` ... ``---`` block if present.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    # Skip past the closing --- and the newline after it
    rest = content[end + 4:]
    return rest.lstrip("\n")
