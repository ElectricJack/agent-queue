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
        written = gen.generate_all()

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

    def test_breadcrumbs(self, tmp_path):
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
            assert "[[vault|Vault]]" in content

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
