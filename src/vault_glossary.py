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

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".obsidian", "__pycache__", ".git"}
_SKIP_FILES = {"index.md"}
_SKIP_PREFIXES = ("spec-", "doc-")

# Regions of text to skip when annotating (code blocks, existing wiki-links)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_WIKI_LINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_FRONTMATTER_RE = re.compile(r"^---\n[\s\S]*?\n---\n", re.MULTILINE)


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
                        raw = line[len("aliases:"):].strip()
                        try:
                            aliases = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            aliases = [a.strip() for a in raw.split(",") if a.strip()]

                # Body after frontmatter
                body = content[end + 4:].strip()
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

        def in_skip_region(pos: int, length: int) -> bool:
            for start, end in skip_regions:
                if pos < end and pos + length > start:
                    return True
            return False

        # Split content into sections by ## headings
        # Process each section independently (first mention per section)
        result = content
        offset = 0  # Track cumulative offset from replacements

        # Sort aliases longest first for greedy matching
        alias_pairs: list[tuple[str, str]] = []
        for alias, concept_name in sorted(
            self._alias_index.items(), key=lambda x: len(x[0]), reverse=True
        ):
            alias_pairs.append((alias, concept_name))

        replaced_in_current: set[str] = set()
        # Track section boundaries
        section_starts = [0]
        for m in re.finditer(r"^##\s", content, re.MULTILINE):
            section_starts.append(m.start())

        for alias, concept_name in alias_pairs:
            if concept_name in replaced_in_current:
                continue

            pattern = re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)
            m = pattern.search(result, offset)
            if not m:
                continue

            # Adjust for offset
            real_pos = m.start()
            if in_skip_region(real_pos, len(alias)):
                # Try to find another occurrence outside skip regions
                pos = m.end()
                found = False
                while True:
                    m = pattern.search(result, pos)
                    if not m:
                        break
                    if not in_skip_region(m.start(), len(alias)):
                        real_pos = m.start()
                        found = True
                        break
                    pos = m.end()
                if not found:
                    continue

            # Replace with wiki-link
            matched_text = result[m.start():m.end()] if m else result[real_pos:real_pos + len(alias)]
            # Use the actual matched text to preserve case
            m2 = pattern.search(result, real_pos)
            if m2:
                matched_text = m2.group(0)
                replacement = f"[[glossary/{concept_name}|{matched_text}]]"
                result = result[:m2.start()] + replacement + result[m2.end():]
                # Update skip regions for the new wiki-link
                skip_regions.append((m2.start(), m2.start() + len(replacement)))
                skip_regions.sort()
                replaced_in_current.add(concept_name)

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

    async def extract_concepts_llm(
        self,
        content: str,
        chat_provider: object,
    ) -> list[dict]:
        """Use LLM to extract key concepts from content.

        Returns list of ``{name, definition, aliases}`` dicts for
        concepts not yet in the glossary.
        """
        prompt = (
            "Extract the key domain concepts from this text. For each concept, provide:\n"
            "- name: a kebab-case identifier (e.g. 'prompt-builder', 'memory-tiers')\n"
            "- definition: a one-sentence definition\n"
            "- aliases: list of variant names/spellings people might use\n\n"
            "Only include concepts that are specific to this domain (not generic programming terms).\n"
            "Return JSON array. Example:\n"
            '[{"name": "smart-cascade", "definition": "Deterministic promotion cascade that runs each 5s cycle.", '
            '"aliases": ["smart cascade", "promotion cascade", "cascade"]}]\n\n'
            f"Text:\n{content[:3000]}"
        )

        try:
            response = await chat_provider.send_message(
                messages=[{"role": "user", "content": prompt}],
                model=getattr(chat_provider, "model", None),
            )
            text = response.get("content", "") if isinstance(response, dict) else str(response)

            # Extract JSON from response
            json_match = re.search(r"\[[\s\S]*\]", text)
            if json_match:
                concepts = json.loads(json_match.group())
                # Filter out already-known concepts
                new_concepts = []
                for c in concepts:
                    name = c.get("name", "")
                    if name and name not in self._concepts:
                        new_concepts.append(c)
                return new_concepts
        except Exception:
            logger.debug("LLM concept extraction failed", exc_info=True)

        return []

    async def generate_initial_glossary(
        self, chat_provider: object, batch_size: int = 5
    ) -> int:
        """Scan all vault files and build the initial glossary.

        1. Reads all non-hub, non-glossary vault files
        2. Extracts concepts per file via LLM
        3. Creates glossary entries for concepts mentioned in 2+ files
        4. Backfills ``## Referenced In`` sections

        Returns count of glossary entries created.
        """
        if not self._loaded:
            self.load()

        # Collect all document content with paths
        docs: list[tuple[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if "glossary" in Path(dirpath).parts:
                continue

            for fname in filenames:
                if not fname.endswith(".md") or fname in _SKIP_FILES:
                    continue
                fp = Path(dirpath) / fname
                rel = str(fp.relative_to(self._root))
                try:
                    content = fp.read_text(encoding="utf-8")
                    docs.append((rel, content))
                except Exception:
                    continue

        # Extract concepts from each document
        concept_mentions: dict[str, dict] = {}  # name → {info, files}
        for rel_path, content in docs:
            try:
                extracted = await self.extract_concepts_llm(content, chat_provider)
            except Exception:
                continue

            for c in extracted:
                name = c.get("name", "")
                if not name:
                    continue
                if name not in concept_mentions:
                    concept_mentions[name] = {
                        "definition": c.get("definition", ""),
                        "aliases": c.get("aliases", []),
                        "files": [],
                    }
                concept_mentions[name]["files"].append(rel_path)

        # Create entries for concepts mentioned in 2+ files
        created = 0
        for name, info in concept_mentions.items():
            if len(info["files"]) < 2:
                continue
            if name in self._concepts:
                continue

            concept = self.add_concept(
                name=name,
                definition=info["definition"],
                aliases=info["aliases"],
            )
            for fp in info["files"]:
                concept.backlinks.append((fp, None))

            # Write with backlinks
            filepath = self._glossary_dir / f"{name}.md"
            filepath.write_text(concept.render(), encoding="utf-8")
            created += 1

        logger.info("Created %d glossary entries", created)
        return created
