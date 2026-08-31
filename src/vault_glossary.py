"""Vault concept glossary for cross-linking the knowledge graph.

Creates and maintains a glossary of concepts in ``vault/glossary/``.
Each concept entry acts as a bridge node in the Obsidian graph,
connecting documents across projects and categories that discuss the
same topic.

Glossary entries have:
- A short definition
- Aliases for fuzzy matching
- A ``## Referenced In`` section with backlinks to all documents
  that mention the concept

Concept detection uses **alias-based matching** (case-insensitive,
word-boundary aware) — fast, deterministic, and predictable.  The
LLM is only used for initial concept extraction, not for matching.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".obsidian", "__pycache__", ".git"}
_SKIP_FILES = {"index.md"}
_SKIP_PREFIXES = ("spec-", "doc-")

# Regions of text to skip when annotating (code blocks, existing wiki-links)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_WIKI_LINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_FRONTMATTER_RE = re.compile(r"^---\n[\s\S]*?\n---\n", re.MULTILINE)


def _parse_alias_field(raw: str) -> list[str]:
    """Parse the ``aliases:`` frontmatter value into a list of alias strings.

    :meth:`GlossaryConcept.render` writes JSON (``["foo", "bar"]``), but
    Obsidian and every other YAML writer emits an unquoted flow list
    (``[foo, bar]``), and hand-edited entries use a bare comma-separated
    line.  All three are accepted.  The flow-list form used to fall through
    to a naive comma split that kept the ``[``/``]`` characters inside the
    aliases, so the concept silently stopped matching any real text.
    """
    if not raw:
        return []

    for loader in (json.loads, yaml.safe_load):
        try:
            parsed = loader(raw)
        except Exception:
            continue
        if isinstance(parsed, list):
            return [str(a).strip() for a in parsed if str(a).strip()]

    # Bare comma-separated fallback.  Strip any surrounding brackets so a
    # flow list neither loader could read still yields clean aliases.
    return [a.strip().strip("[]").strip() for a in raw.split(",") if a.strip().strip("[]").strip()]


@dataclass
class GlossaryConcept:
    """A single concept in the glossary."""

    name: str  # Canonical name (also the filename stem)
    definition: str  # Short definition
    aliases: list[str] = field(default_factory=list)  # Match variants
    backlinks: list[tuple[str, str | None]] = field(
        default_factory=list
    )  # (vault_rel_path, section_hint)

    @property
    def filename(self) -> str:
        """Filename for this concept (without extension)."""
        return self.name

    def render(self) -> str:
        """Render as a markdown file with frontmatter."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        aliases_json = json.dumps(self.aliases)

        lines = [
            "---",
            "tags: [glossary, concept]",
            "type: glossary",
            f"aliases: {aliases_json}",
            f"updated: {now}",
            "---",
            "",
            f"# {self.name.replace('-', ' ').title()}",
            "",
            self.definition,
            "",
        ]

        if self.backlinks:
            lines.append("## Referenced In")
            seen = set()
            for path, section in self.backlinks:
                if path in seen:
                    continue
                seen.add(path)
                stem = Path(path).stem
                display = stem
                suffix = f" — § {section}" if section else ""
                lines.append(f"- [[{Path(path).with_suffix('')}|{display}]]{suffix}")
            lines.append("")

        return "\n".join(lines)


class VaultGlossary:
    """Concept glossary for the vault knowledge graph.

    Manages glossary entries in ``vault/glossary/``, provides alias-based
    concept matching, content annotation with wiki-links, and backlink
    maintenance.
    """

    def __init__(self, vault_root: str | Path):
        self._root = Path(vault_root)
        self._glossary_dir = self._root / "glossary"
        self._concepts: dict[str, GlossaryConcept] = {}  # name → concept
        self._alias_index: dict[str, str] = {}  # lowercase alias → concept name
        self._loaded = False

    @property
    def glossary_dir(self) -> Path:
        return self._glossary_dir

    def load(self) -> None:
        """Load all glossary entries and build alias index."""
        self._concepts.clear()
        self._alias_index.clear()

        if not self._glossary_dir.exists():
            self._loaded = True
            return

        for fp in sorted(self._glossary_dir.glob("*.md")):
            if fp.name == "index.md":
                continue
            concept = self._parse_glossary_file(fp)
            if concept:
                self._concepts[concept.name] = concept
                for alias in concept.aliases:
                    self._alias_index[alias.lower()] = concept.name

        self._loaded = True
        logger.debug("Loaded %d glossary concepts", len(self._concepts))

    def _parse_glossary_file(self, filepath: Path) -> GlossaryConcept | None:
        """Parse a glossary markdown file into a GlossaryConcept."""
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            return None

        name = filepath.stem
        aliases: list[str] = []
        definition = ""
        backlinks: list[tuple[str, str | None]] = []

        # Parse frontmatter
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                fm = content[4:end]
                for line in fm.split("\n"):
                    if line.startswith("aliases:"):
                        aliases = _parse_alias_field(line[len("aliases:") :].strip())

                # Body after frontmatter
                body = content[end + 4 :].strip()
            else:
                body = content
        else:
            body = content

        # Extract definition (text between title and ## Referenced In)
        lines = body.split("\n")
        def_lines = []
        in_refs = False
        for line in lines:
            if line.startswith("# "):
                continue  # Skip title
            if line.startswith("## Referenced In"):
                in_refs = True
                continue
            if in_refs:
                # Parse backlinks
                m = re.match(r"- \[\[([^\]|]+?)(?:\|[^\]]*?)?\]\](?:\s*—\s*§\s*(.+))?", line)
                if m:
                    backlinks.append((m.group(1) + ".md", m.group(2)))
            elif not in_refs:
                def_lines.append(line)

        definition = "\n".join(def_lines).strip()

        # Ensure the concept name itself is an alias
        name_variants = [name, name.replace("-", " "), name.replace("-", "_")]
        for v in name_variants:
            if v.lower() not in [a.lower() for a in aliases]:
                aliases.append(v)

        return GlossaryConcept(
            name=name,
            definition=definition,
            aliases=aliases,
            backlinks=backlinks,
        )

    def find_concepts(self, text: str) -> list[GlossaryConcept]:
        """Find which glossary concepts are mentioned in text.

        Uses alias matching (case-insensitive, word-boundary aware).
        Returns concepts sorted by specificity (longer aliases first).
        """
        if not self._loaded:
            self.load()

        found: dict[str, GlossaryConcept] = {}
        text_lower = text.lower()

        # Sort aliases by length (longest first) for greedy matching
        sorted_aliases = sorted(self._alias_index.keys(), key=len, reverse=True)

        for alias in sorted_aliases:
            concept_name = self._alias_index[alias]
            if concept_name in found:
                continue  # Already found via a longer alias

            # Word-boundary match
            pattern = r"\b" + re.escape(alias) + r"\b"
            if re.search(pattern, text_lower):
                concept = self._concepts.get(concept_name)
                if concept:
                    found[concept_name] = concept

        return list(found.values())

    def annotate_content(self, content: str) -> str:
        """Replace first mention of each concept with a wiki-link.

        Only links the FIRST mention per concept per section (## heading)
        to avoid over-linking.  Skips text inside existing wiki-links,
        code blocks, and frontmatter.
        """
        if not self._loaded:
            self.load()
        if not self._concepts:
            return content

        # Find regions to skip (code blocks, inline code, existing links, frontmatter)
        skip_regions: list[tuple[int, int]] = []
        for pattern in [_FRONTMATTER_RE, _CODE_BLOCK_RE, _INLINE_CODE_RE, _WIKI_LINK_RE]:
            for m in pattern.finditer(content):
                skip_regions.append((m.start(), m.end()))
        skip_regions.sort()

        def in_skip_region(pos: int, end_pos: int) -> bool:
            for start, end in skip_regions:
                if pos < end and end_pos > start:
                    return True
            return False

        # A top-level preamble is a section too.  Ignore apparent headings in
        # protected regions (most importantly fenced code blocks).
        section_starts = [0]
        for m in re.finditer(r"^##\s", content, re.MULTILINE):
            if not in_skip_region(m.start(), m.end()):
                section_starts.append(m.start())
        section_starts.append(len(content))

        # Find replacements against the original text, then apply them from
        # right to left.  Original coordinates remain valid and protected
        # regions do not need adjustment after every inserted wiki-link.
        replacements: list[tuple[int, int, str]] = []
        alias_pairs = sorted(self._alias_index.items(), key=lambda item: len(item[0]), reverse=True)

        for section_start, section_end in zip(section_starts, section_starts[1:]):
            first_by_concept: dict[str, tuple[int, int, str]] = {}
            for alias, concept_name in alias_pairs:
                pattern = re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)
                for match in pattern.finditer(content, section_start, section_end):
                    if in_skip_region(match.start(), match.end()):
                        continue
                    candidate = (match.start(), match.end(), match.group(0))
                    previous = first_by_concept.get(concept_name)
                    if (
                        previous is None
                        or candidate[0] < previous[0]
                        or (
                            candidate[0] == previous[0]
                            and (candidate[1] - candidate[0]) > (previous[1] - previous[0])
                        )
                    ):
                        first_by_concept[concept_name] = candidate
                    break

            # Prefer the longest match when aliases for different concepts
            # overlap at the same location, and never emit overlapping links.
            section_candidates = sorted(
                (
                    (start, end, f"[[glossary/{concept_name}|{matched_text}]]")
                    for concept_name, (start, end, matched_text) in first_by_concept.items()
                ),
                key=lambda item: (item[0], -(item[1] - item[0])),
            )
            last_end = section_start
            for candidate in section_candidates:
                if candidate[0] < last_end:
                    continue
                replacements.append(candidate)
                last_end = candidate[1]

        result = content
        for start, end, replacement in reversed(replacements):
            result = result[:start] + replacement + result[end:]
        return result

    def update_backlinks(
        self, concept_name: str, source_path: str, section: str | None = None
    ) -> None:
        """Add a backlink from a glossary entry to a source document."""
        if not self._loaded:
            self.load()

        concept = self._concepts.get(concept_name)
        if not concept:
            return

        # Check if backlink already exists
        for existing_path, _ in concept.backlinks:
            if existing_path == source_path:
                return

        concept.backlinks.append((source_path, section))

        # Write updated glossary file
        self._glossary_dir.mkdir(parents=True, exist_ok=True)
        filepath = self._glossary_dir / f"{concept.filename}.md"
        filepath.write_text(concept.render(), encoding="utf-8")

    def add_concept(
        self,
        name: str,
        definition: str,
        aliases: list[str] | None = None,
    ) -> GlossaryConcept:
        """Add a new concept to the glossary."""
        self._glossary_dir.mkdir(parents=True, exist_ok=True)

        # Build aliases
        all_aliases = list(aliases or [])
        name_variants = [name, name.replace("-", " "), name.replace("-", "_")]
        for v in name_variants:
            if v.lower() not in [a.lower() for a in all_aliases]:
                all_aliases.append(v)

        concept = GlossaryConcept(
            name=name,
            definition=definition,
            aliases=all_aliases,
        )

        filepath = self._glossary_dir / f"{name}.md"
        filepath.write_text(concept.render(), encoding="utf-8")

        self._concepts[name] = concept
        for alias in all_aliases:
            self._alias_index[alias.lower()] = name

        return concept

    def annotate_all_safe_files(self) -> int:
        """Annotate all safe vault files with glossary concept links.

        Skips auto-generated files, index files, and glossary files.
        Only modifies files whose content actually changes.

        Returns count of files modified.
        """
        if not self._loaded:
            self.load()
        if not self._concepts:
            return 0

        modified = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if "glossary" in Path(dirpath).parts:
                continue

            rel_dir = os.path.relpath(dirpath, self._root)
            if any(part in _SKIP_DIRS for part in Path(rel_dir).parts if rel_dir != "."):
                continue

            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                if fname in _SKIP_FILES or fname == "facts.md":
                    continue
                if any(fname.startswith(p) for p in _SKIP_PREFIXES):
                    continue

                filepath = Path(dirpath) / fname
                content = filepath.read_text(encoding="utf-8")
                new_content = self.annotate_content(content)

                if new_content != content:
                    filepath.write_text(new_content, encoding="utf-8")
                    modified += 1

                    # Update backlinks for found concepts
                    rel_path = str(filepath.relative_to(self._root))
                    concepts = self.find_concepts(content)
                    for concept in concepts:
                        self.update_backlinks(concept.name, rel_path)

        logger.info("Annotated %d vault files with glossary links", modified)
        return modified
