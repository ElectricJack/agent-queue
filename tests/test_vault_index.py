"""Tests for VaultIndexGenerator."""

from src.vault_index import VaultIndexGenerator, _display_name, _file_display_name


class TestDisplayName:
    def test_known_names(self):
        assert _display_name("agent-types") == "Agent Types"
        assert _display_name("code-review") == "Code Review"

    def test_unknown_name(self):
        assert _display_name("some-thing") == "Some Thing"


class TestFileDisplayName:
    def test_simple(self):
        assert _file_display_name("my-file.md") == "my-file"

    def test_strips_hash(self):
        result = _file_display_name("some-insight-abc123.md")
        assert result == "some-insight"

    def test_truncates_long(self):
        name = "a" * 80 + ".md"
        result = _file_display_name(name)
        assert len(result) <= 60


class TestVaultIndexGenerator:
    def test_generates_root_hub(self, tmp_path):
        # Create vault-like structure with a recognizable root name
        vault = tmp_path / "vault"
        vault.mkdir()
        sub = vault / "projects"
        sub.mkdir()
        (sub / "readme.md").write_text("# Projects")
        sub2 = vault / "system"
        sub2.mkdir()
        (sub2 / "playbook.md").write_text("# Playbook")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        # Root hub named after directory: vault.md
        root_hub = vault / "vault.md"
        assert root_hub.exists()
        content = root_hub.read_text()
        assert "# Vault" in content
        assert "[[projects/readme|Projects]]" in content

    def test_hub_named_after_directory(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        projects = vault / "projects"
        p1 = projects / "proj-a"
        p1.mkdir(parents=True)
        (p1 / "readme.md").write_text("# A")
        p2 = projects / "proj-b"
        p2.mkdir(parents=True)
        (p2 / "readme.md").write_text("# B")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        # Hub should be projects.md, not index.md
        assert (projects / "projects.md").exists()
        assert not (projects / "index.md").exists()

    def test_skips_dir_with_root_file(self, tmp_path):
        vault = tmp_path / "vault"
        agent_dir = vault / "agent-types" / "coding"
        agent_dir.mkdir(parents=True)
        (agent_dir / "profile.md").write_text("# Profile")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        assert not (agent_dir / "coding.md").exists()

    def test_creates_hub_for_large_dir(self, tmp_path):
        vault = tmp_path / "vault"
        refs = vault / "references"
        refs.mkdir(parents=True)
        for i in range(15):
            (refs / f"spec-{i}.md").write_text(f"# Spec {i}")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        assert (refs / "references.md").exists()

    def test_groups_reference_stubs(self, tmp_path):
        vault = tmp_path / "vault"
        refs = vault / "references"
        refs.mkdir(parents=True)
        for name in ["spec-design-foo.md", "spec-bar.md", "doc-guide.md"]:
            (refs / name).write_text(f"# {name}")
        for i in range(10):
            (refs / f"doc-extra-{i}.md").write_text(f"# Extra {i}")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        content = (refs / "references.md").read_text()
        assert "Specs — Design" in content
        assert "Specs — Components" in content
        assert "Documentation" in content

    def test_parent_link_only(self, tmp_path):
        """Hub files link to immediate parent only, not full chain."""
        vault = tmp_path / "vault"
        deep = vault / "projects" / "my-proj" / "memory"
        insights = deep / "insights"
        insights.mkdir(parents=True)
        for i in range(12):
            (insights / f"insight-{i}.md").write_text(f"# Insight {i}")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        if (deep / "memory.md").exists():
            content = (deep / "memory.md").read_text()
            # Should have parent link to my-proj, not full chain
            assert "Parent:" in content
            # Should NOT have vault root in the parent line
            assert "[[vault|" not in content

    def test_update_directory(self, tmp_path):
        vault = tmp_path / "vault"
        sub = vault / "notes"
        sub.mkdir(parents=True)
        for i in range(12):
            (sub / f"note-{i}.md").write_text(f"# Note {i}")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()

        (sub / "note-new.md").write_text("# New Note")
        gen.update_directory("notes")

        content = (sub / "notes.md").read_text()
        assert "note-new" in content

    def test_index_update_after_spec_stub_write_preserves_summary(self, tmp_path):
        """The watcher handoff converges without clobbering a rebuilt summary.

        The orchestrator runs the spec watcher after the vault watcher.  Its
        new stub is therefore indexed by the vault watcher's next debounced
        update, which must retain any LLM summary already stored in the hub.
        """
        vault = tmp_path / "vault"
        refs = vault / "projects" / "proj" / "references"
        refs.mkdir(parents=True)
        for i in range(10):
            (refs / f"spec-{i}.md").write_text(f"# Spec {i}")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()
        gen._generate_hub_for_dir(
            str(refs),
            "projects/proj/references",
            summary="Summarizes the project specifications.",
        )

        (refs / "spec-new.md").write_text("# New")
        gen.update_directory("projects/proj/references")

        content = (refs / "references.md").read_text()
        assert "Summarizes the project specifications." in content
        assert "[[projects/proj/references/spec-new|spec-new]]" in content


class TestMigrateBacklinks:
    def test_adds_backlinks(self, tmp_path):
        vault = tmp_path / "vault"
        sub = vault / "notes"
        sub.mkdir(parents=True)
        (sub / "my-note.md").write_text("# My Note\nSome content")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()
        count = gen.migrate_backlinks()

        content = (sub / "my-note.md").read_text()
        assert "## See Also" in content
        assert count >= 1

    def test_skips_auto_generated(self, tmp_path):
        vault = tmp_path / "vault"
        sub = vault / "memory"
        sub.mkdir(parents=True)
        (sub / "facts.md").write_text("key: value")

        gen = VaultIndexGenerator(vault)
        gen.generate_all()
        gen.migrate_backlinks()

        content = (sub / "facts.md").read_text()
        assert "## See Also" not in content

    def test_skips_existing_see_also(self, tmp_path):
        vault = tmp_path / "vault"
        sub = vault / "notes"
        sub.mkdir(parents=True)
        original = "# Note\n\n## See Also\n- existing"
        (sub / "note.md").write_text(original)

        gen = VaultIndexGenerator(vault)
        gen.generate_all()
        gen.migrate_backlinks()

        content = (sub / "note.md").read_text()
        assert content == original


class TestGenerateAllWithSummaries:
    async def test_generate_all_with_summaries_preserves_summary_through_pass_three(self, tmp_path):
        """Pass 3 rewrites breadcrumbs without clobbering pass 2's summaries.

        Both passes write the same hub files; the summary only survives
        because pass 3 re-reads it out of the file it is about to replace.
        """
        from src.llm import LLMClient
        from src.llm.fake import FakeProvider

        memory = tmp_path / "memory"
        deep = memory / "deep"
        deep.mkdir(parents=True)
        for i in range(3):
            (memory / f"note{i}.md").write_text(f"# Note {i}\n\nshallow knowledge {i}\n")
        # A leaf directory only earns a hub once it is large; ``memory`` earns
        # one because it has a subdirectory.
        for i in range(10):
            (deep / f"deep{i}.md").write_text(f"# Deep {i}\n\nnested knowledge {i}\n")

        fake = FakeProvider()
        # os.walk is bottom-up in pass 1, so ``deep`` is summarised first.
        fake.add_text('"Covers the nested knowledge."')
        fake.add_text('"Covers the shallow knowledge."')
        fake.add_text('"Covers the vault root."')

        gen = VaultIndexGenerator(str(tmp_path))
        written = await gen.generate_all_with_summaries(LLMClient.with_provider(fake))

        deep_hub = deep / "deep.md"
        memory_hub = memory / "memory.md"
        assert deep_hub.exists() and memory_hub.exists()
        assert str(deep_hub) in written and str(memory_hub) in written

        deep_text = deep_hub.read_text()
        memory_text = memory_hub.read_text()
        assert "Covers the nested knowledge." in deep_text
        assert "Covers the shallow knowledge." in memory_text
        # Pass 3 ran: the child hub now carries a parent breadcrumb.
        assert "Parent:" in deep_text
        # And the summary survived that rewrite verbatim.
        assert gen._extract_summary(deep_hub) == "Covers the nested knowledge."
        assert gen._extract_summary(memory_hub) == "Covers the shallow knowledge."
