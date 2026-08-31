"""Tests for VaultGlossary concept matching and annotation."""

from src.vault_glossary import GlossaryConcept, VaultGlossary


class TestGlossaryConcept:
    def test_render(self):
        concept = GlossaryConcept(
            name="smart-cascade",
            definition="Deterministic promotion cascade.",
            aliases=["smart cascade", "promotion cascade"],
        )
        rendered = concept.render()
        assert "# Smart Cascade" in rendered
        assert "Deterministic promotion cascade." in rendered
        assert "tags: [glossary, concept]" in rendered
        assert '"smart cascade"' in rendered

    def test_render_with_backlinks(self):
        concept = GlossaryConcept(
            name="reflection",
            definition="Post-task review system.",
            aliases=["reflection", "reflection engine"],
            backlinks=[
                ("projects/foo/notes/bar.md", "error handling"),
                ("system/playbooks/task-outcome.md", None),
            ],
        )
        rendered = concept.render()
        assert "## Referenced In" in rendered
        assert "projects/foo/notes/bar" in rendered
        assert "§ error handling" in rendered


class TestVaultGlossary:
    def test_add_and_load(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="test-concept",
            definition="A test concept.",
            aliases=["test concept", "TC"],
        )

        # Reload
        g2 = VaultGlossary(tmp_path)
        g2.load()
        assert "test-concept" in g2._concepts
        assert g2._concepts["test-concept"].definition == "A test concept."

    def test_find_concepts(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="playbooks",
            definition="DAG workflows.",
            aliases=["playbooks", "playbook"],
        )
        glossary.add_concept(
            name="reflection",
            definition="Post-task review.",
            aliases=["reflection", "reflection engine"],
        )

        found = glossary.find_concepts("The playbook system uses reflection engine for review.")
        names = {c.name for c in found}
        assert "playbooks" in names
        assert "reflection" in names

    def test_find_concepts_no_match(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="foo", definition="Foo.", aliases=["foo"])
        assert glossary.find_concepts("no match here") == []

    def test_annotate_content(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="pytest-asyncio",
            definition="Async test framework.",
            aliases=["pytest-asyncio", "pytest asyncio"],
        )

        content = "Use pytest-asyncio for testing. And pytest-asyncio again."
        result = glossary.annotate_content(content)
        # First mention replaced, second not
        assert "[[glossary/pytest-asyncio|pytest-asyncio]]" in result
        # Should appear only once as a wiki-link
        assert result.count("[[glossary/pytest-asyncio|") == 1

    def test_annotate_links_first_mention_in_each_h2_section(self, tmp_path):
        """A concept may be linked once in every ``##`` section, not once per file."""
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="widget", definition="A unit.", aliases=["widget"])

        content = (
            "Preamble widget and widget again.\n\n"
            "## Alpha\n\nAlpha widget and widget again.\n\n"
            "### Nested\n\nNested widget stays in Alpha.\n\n"
            "## Beta\n\nBeta widget and widget again.\n"
        )

        result = glossary.annotate_content(content)

        assert result.count("[[glossary/widget|widget]]") == 3
        assert "Preamble [[glossary/widget|widget]] and widget again." in result
        assert "Alpha [[glossary/widget|widget]] and widget again." in result
        assert "Nested widget stays in Alpha." in result
        assert "Beta [[glossary/widget|widget]] and widget again." in result

    def test_annotate_skips_code_blocks(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="foo", definition="Foo.", aliases=["foo"])

        content = "```\nfoo in code\n```\nfoo outside"
        result = glossary.annotate_content(content)
        assert "[[glossary/foo|foo]]" in result
        # The code block should not be modified
        assert "```\nfoo in code\n```" in result

    def test_annotate_skips_existing_links(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="bar", definition="Bar.", aliases=["bar"])

        content = "[[bar|existing link]] and bar outside"
        result = glossary.annotate_content(content)
        # Should add link for the second mention
        assert "[[glossary/bar|bar]]" in result

    def test_update_backlinks(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="test", definition="Test.", aliases=["test"])
        glossary.update_backlinks("test", "projects/foo/notes/bar.md")

        # Reload and verify
        g2 = VaultGlossary(tmp_path)
        g2.load()
        assert len(g2._concepts["test"].backlinks) == 1
        assert g2._concepts["test"].backlinks[0][0] == "projects/foo/notes/bar.md"

    def test_annotate_preserves_frontmatter(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(name="vault", definition="File storage.", aliases=["vault"])

        content = "---\ntags: [vault]\n---\n\nvault is great"
        result = glossary.annotate_content(content)
        # Frontmatter should be preserved
        assert result.startswith("---\ntags: [vault]\n---")
        # Body mention should be linked
        assert "[[glossary/vault|vault]]" in result


class TestAnnotateAllSafeFiles:
    def test_annotate_all_safe_files_skips_facts_hubs_and_glossary(self, tmp_path):
        """Only ordinary notes are rewritten; generated/hub files are left alone."""
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="widget",
            definition="A unit of work.",
            aliases=["widget"],
        )

        note = tmp_path / "projects" / "p" / "notes.md"
        note.parent.mkdir(parents=True)
        body = "We ship one widget per release.\n"
        note.write_text(body)

        untouched = {
            tmp_path / "projects" / "p" / "facts.md": "A widget is tracked here.\n",
            tmp_path / "projects" / "p" / "spec-overview.md": "The widget spec.\n",
            tmp_path / "projects" / "p" / "index.md": "Every widget, listed.\n",
            tmp_path / ".obsidian" / "cache.md": "widget cache\n",
        }
        for path, text in untouched.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        modified = VaultGlossary(tmp_path).annotate_all_safe_files()

        assert modified == 1
        assert "[[glossary/widget|widget]]" in note.read_text()
        for path, text in untouched.items():
            assert path.read_text() == text, f"{path.name} should not have been rewritten"

        entry = (tmp_path / "glossary" / "widget.md").read_text()
        assert "## Referenced In" in entry
        assert "projects/p/notes" in entry


class TestFlowListAliases:
    def test_flow_list_aliases_link_concept(self, tmp_path):
        """``aliases: [foo, bar]`` is a YAML flow list, not JSON (VP-3).

        Obsidian writes this form.  Parsing it with a bare comma split kept
        the brackets inside the aliases, so the concept silently stopped
        matching any real text.
        """
        glossary_dir = tmp_path / "glossary"
        glossary_dir.mkdir()
        (glossary_dir / "widget.md").write_text(
            "---\n"
            "tags: [glossary, concept]\n"
            "aliases: [foo, bar]\n"
            "---\n\n"
            "# Widget\n\n"
            "A unit of work.\n"
        )

        glossary = VaultGlossary(tmp_path)
        glossary.load()

        concept = glossary._concepts["widget"]
        assert "foo" in concept.aliases
        assert "bar" in concept.aliases
        assert not any("[" in a or "]" in a for a in concept.aliases)

        assert [c.name for c in glossary.find_concepts("we use foo here")] == ["widget"]
        assert [c.name for c in glossary.find_concepts("a bar walks in")] == ["widget"]
        assert glossary.annotate_content("we use foo here") == (
            "we use [[glossary/widget|foo]] here"
        )
        assert glossary.annotate_content("a bar walks in") == "a [[glossary/widget|bar]] walks in"
