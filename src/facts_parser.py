"""Standalone parser for ``facts.md`` files — KV source of truth.

Parses the human-readable markdown format used by the vault facts files
into ``{namespace: {key: value}}`` dictionaries, and renders them back.

The format (per spec ``docs/specs/design/memory-plugin.md`` Section 7)::

    ---
    tags: [facts, auto-updated]
    ---

    # Project Facts -- My Project

    ## Project
    - tech_stack: [Python 3.12, SQLAlchemy, Pygame]
    - deploy_branch: main
    - test_command: pytest tests/ -v

    ## Conventions
    - orm_pattern: repository
    - naming: snake_case

Rules:

- ``## heading`` lines define **namespaces**.
- Lines under a namespace with a colon (``key: value``) become KV entries.
- Leading bullet markers (``-``, ``*``, ``+``) are stripped from KV lines.
- Only the **first** colon on a line is the separator; values may contain colons.
- Lines before the first ``## heading`` are ignored (title, frontmatter, etc.).
- Lines starting with ``#`` (headings) are not treated as KV entries.
- YAML frontmatter (``---`` blocks) at the start of the file is skipped.
- Blank lines and non-KV lines (no colon) are silently ignored.

The parser is deterministic — no LLM required.
"""

from __future__ import annotations

import re

# Regex for optional leading bullet: - , * , or + followed by space(s).
_BULLET_RE = re.compile(r"^[-*+]\s+")


def parse_facts_file(text: str) -> dict[str, dict[str, str]]:
    """Parse a ``facts.md`` file into ``{namespace: {key: value}}``.

    Parameters
    ----------
    text:
        Raw content of the facts.md file (UTF-8 string).

    Returns
    -------
    dict[str, dict[str, str]]
        Mapping of namespace -> {key -> value}.  Empty namespaces are
        included if the heading was present but had no entries.

    Examples
    --------
    >>> parse_facts_file("## project\\ntech_stack: Python\\n")
    {'project': {'tech_stack': 'Python'}}

    >>> parse_facts_file("## urls\\n- api: http://localhost:8080\\n")
    {'urls': {'api': 'http://localhost:8080'}}
    """
    result: dict[str, dict[str, str]] = {}
    current_ns: str | None = None
    in_frontmatter = False
    frontmatter_seen = False

    for line in text.splitlines():
        stripped = line.strip()

        # --- YAML frontmatter handling ---
        # Frontmatter is a ``---`` block at the very start of the file.
        if stripped == "---":
            if not frontmatter_seen:
                if not in_frontmatter:
                    # Opening ``---``
                    in_frontmatter = True
                    continue
                else:
                    # Closing ``---``
                    in_frontmatter = False
                    frontmatter_seen = True
                    continue
            # A ``---`` after frontmatter is just a horizontal rule; ignore.
            continue

        if in_frontmatter:
            continue

        # --- Namespace heading ---
        if stripped.startswith("## "):
            ns = stripped[3:].strip()
            if ns:
                current_ns = ns
                if current_ns not in result:
                    result[current_ns] = {}
            continue

        # Skip other headings (# title, ### sub-heading, etc.)
        if stripped.startswith("#"):
            continue

        # Skip blank lines
        if not stripped:
            continue

        # --- KV line ---
        if current_ns is None:
            # No namespace heading seen yet; ignore orphan lines
            continue

        # Strip optional bullet prefix: - , * , +
        kv_line = _BULLET_RE.sub("", stripped)

        if ":" not in kv_line:
            continue

        colon_idx = kv_line.index(":")
        key = kv_line[:colon_idx].strip()
        value = kv_line[colon_idx + 1 :].strip()

        if key:
            result[current_ns][key] = value

    return result


def render_facts_file(
    data: dict[str, dict[str, str]],
    frontmatter: dict[str, object] | None = None,
) -> str:
    """Render a ``{namespace: {key: value}}`` dict to facts.md format.

    Namespaces and keys are sorted alphabetically for deterministic output.

    Parameters
    ----------
    data:
        Mapping of namespace -> {key -> value}.
    frontmatter:
        Optional YAML frontmatter properties to include at the top of the
        file.  When provided, a ``---`` delimited block is prepended.

    Returns
    -------
    str
        Formatted markdown content.  Empty string if *data* is empty
        and no frontmatter is provided.

    Examples
    --------
    >>> render_facts_file({"project": {"lang": "Python", "db": "SQLite"}})
    '## project\\ndb: SQLite\\nlang: Python\\n'
    """
    if not data and not frontmatter:
        return ""

    parts: list[str] = []

    if frontmatter:
        import json as _json

        fm_lines = ["---"]
        for key in sorted(frontmatter.keys()):
            val = frontmatter[key]
            if isinstance(val, (list, dict)):
                fm_lines.append(f"{key}: {_json.dumps(val)}")
            else:
                fm_lines.append(f"{key}: {val}")
        fm_lines.append("---")
        fm_lines.append("")
        parts.append("\n".join(fm_lines))

    sections: list[str] = []
    for ns in sorted(data.keys()):
        entries = data[ns]
        lines = [f"## {ns}"]
        for k in sorted(entries.keys()):
            lines.append(f"{k}: {entries[k]}")
        sections.append("\n".join(lines))

    if sections:
        parts.append("\n\n".join(sections) + "\n")

    return "\n".join(parts) if parts else ""


def extract_preamble(text: str) -> tuple[str, str]:
    """Split a facts.md file into preamble and structured body.

    The **preamble** is everything before the first ``## heading`` line
    (YAML frontmatter, ``# title``, blank lines, etc.).  The **body** is
    everything from the first ``## heading`` onward.

    YAML frontmatter blocks (``---`` delimiters) are handled correctly —
    a ``## `` sequence inside frontmatter is not treated as a heading.

    Parameters
    ----------
    text:
        Raw content of the facts.md file (UTF-8 string).

    Returns
    -------
    tuple[str, str]
        ``(preamble, body)`` where either may be empty.

    Examples
    --------
    >>> extract_preamble("---\\ntags: [facts]\\n---\\n\\n## project\\nkey: val\\n")
    ('---\\ntags: [facts]\\n---\\n\\n', '## project\\nkey: val\\n')

    >>> extract_preamble("## project\\nkey: val\\n")
    ('', '## project\\nkey: val\\n')
    """
    lines = text.splitlines(keepends=True)
    in_frontmatter = False
    frontmatter_seen = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # --- YAML frontmatter tracking (mirrors parse_facts_file logic) ---
        if stripped == "---":
            if not frontmatter_seen:
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                else:
                    in_frontmatter = False
                    frontmatter_seen = True
                    continue
            # Post-frontmatter ``---`` is a horizontal rule; skip
            continue

        if in_frontmatter:
            continue

        # First ``## heading`` outside frontmatter marks the body start
        if stripped.startswith("## ") and stripped[3:].strip():
            preamble = "".join(lines[:i])
            body = "".join(lines[i:])
            return preamble, body

    # No ``## heading`` found — everything is preamble
    return text, ""


def diff_facts(
    old: dict[str, dict[str, str]],
    new: dict[str, dict[str, str]],
) -> tuple[
    dict[str, dict[str, str]],  # upserts  (namespace -> {key -> value})
    dict[str, list[str]],  # deletes  (namespace -> [keys])
]:
    """Compute the delta between two parsed facts states.

    Parameters
    ----------
    old:
        The previous parsed state (may be empty for a new file).
    new:
        The current parsed state.

    Returns
    -------
    tuple[dict, dict]
        A ``(upserts, deletes)`` pair.  *upserts* contains entries that
        were added or whose value changed.  *deletes* contains keys that
        were present in *old* but absent in *new*.

    Examples
    --------
    >>> old = {"project": {"a": "1", "b": "2"}}
    >>> new = {"project": {"a": "1", "c": "3"}}
    >>> upserts, deletes = diff_facts(old, new)
    >>> upserts
    {'project': {'c': '3'}}
    >>> deletes
    {'project': ['b']}
    """
    upserts: dict[str, dict[str, str]] = {}
    deletes: dict[str, list[str]] = {}

    all_namespaces = set(old.keys()) | set(new.keys())

    for ns in all_namespaces:
        old_entries = old.get(ns, {})
        new_entries = new.get(ns, {})

        # Upserts: keys in new that are missing or changed from old
        ns_upserts: dict[str, str] = {}
        for k, v in new_entries.items():
            if k not in old_entries or old_entries[k] != v:
                ns_upserts[k] = v
        if ns_upserts:
            upserts[ns] = ns_upserts

        # Deletes: keys in old that are missing from new
        ns_deletes = [k for k in old_entries if k not in new_entries]
        if ns_deletes:
            deletes[ns] = ns_deletes

    return upserts, deletes
