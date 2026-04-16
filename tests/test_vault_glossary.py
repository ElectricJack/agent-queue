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
        glossary.add_concept(
            name="foo", definition="Foo.", aliases=["foo"]
        )
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

    def test_annotate_skips_code_blocks(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="foo", definition="Foo.", aliases=["foo"]
        )

        content = "```\nfoo in code\n```\nfoo outside"
        result = glossary.annotate_content(content)
        assert "[[glossary/foo|foo]]" in result
        # The code block should not be modified
        assert "```\nfoo in code\n```" in result

    def test_annotate_skips_existing_links(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="bar", definition="Bar.", aliases=["bar"]
        )

        content = "[[bar|existing link]] and bar outside"
        result = glossary.annotate_content(content)
        # Should add link for the second mention
        assert "[[glossary/bar|bar]]" in result

    def test_update_backlinks(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        concept = glossary.add_concept(
            name="test", definition="Test.", aliases=["test"]
        )
        glossary.update_backlinks("test", "projects/foo/notes/bar.md")

        # Reload and verify
        g2 = VaultGlossary(tmp_path)
        g2.load()
        assert len(g2._concepts["test"].backlinks) == 1
        assert g2._concepts["test"].backlinks[0][0] == "projects/foo/notes/bar.md"

    def test_annotate_preserves_frontmatter(self, tmp_path):
        glossary = VaultGlossary(tmp_path)
        glossary.add_concept(
            name="vault", definition="File storage.", aliases=["vault"]
        )

        content = "---\ntags: [vault]\n---\n\nvault is great"
        result = glossary.annotate_content(content)
        # Frontmatter should be preserved
        assert result.startswith("---\ntags: [vault]\n---")
        # Body mention should be linked
        assert "[[glossary/vault|vault]]" in result
