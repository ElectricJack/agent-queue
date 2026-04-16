"""Tests for wiki-link parser, resolver, and annotation utilities."""

import tempfile
from pathlib import Path

from src.wiki_links import (
    add_see_also,
    parse_wiki_links,
    resolve_wiki_link,
    strip_frontmatter,
    vault_path_to_wiki_link,
)


class TestParseWikiLinks:
    def test_simple_link(self):
        result = parse_wiki_links("See [[foo/bar]]")
        assert result == [{"target": "foo/bar", "display": "bar"}]

    def test_link_with_display(self):
        result = parse_wiki_links("See [[foo/bar|My Display]]")
        assert result == [{"target": "foo/bar", "display": "My Display"}]

    def test_multiple_links(self):
        result = parse_wiki_links("[[a]] and [[b/c|C]]")
        assert len(result) == 2
        assert result[0] == {"target": "a", "display": "a"}
        assert result[1] == {"target": "b/c", "display": "C"}

    def test_no_links(self):
        assert parse_wiki_links("No links here") == []

    def test_deduplicates(self):
        result = parse_wiki_links("[[foo]] and [[foo]]")
        assert len(result) == 1

    def test_empty_string(self):
        assert parse_wiki_links("") == []


class TestResolveWikiLink:
    def test_exact_path(self, tmp_path):
        (tmp_path / "foo.md").write_text("content")
        assert resolve_wiki_link(tmp_path, "foo.md") == tmp_path / "foo.md"

    def test_with_md_extension(self, tmp_path):
        (tmp_path / "bar.md").write_text("content")
        result = resolve_wiki_link(tmp_path, "bar")
        assert result == tmp_path / "bar.md"

    def test_nested_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file.md").write_text("content")
        result = resolve_wiki_link(tmp_path, "sub/file")
        assert result == tmp_path / "sub" / "file.md"

    def test_filename_search(self, tmp_path):
        (tmp_path / "deep" / "nested").mkdir(parents=True)
        (tmp_path / "deep" / "nested" / "target.md").write_text("content")
        result = resolve_wiki_link(tmp_path, "target")
        assert result == tmp_path / "deep" / "nested" / "target.md"

    def test_not_found(self, tmp_path):
        assert resolve_wiki_link(tmp_path, "nonexistent") is None


class TestVaultPathToWikiLink:
    def test_absolute_path(self):
        result = vault_path_to_wiki_link("/vault", "/vault/projects/foo/bar.md")
        assert result == "projects/foo/bar"

    def test_relative_path(self):
        result = vault_path_to_wiki_link("/vault", "projects/foo/bar.md")
        assert result == "projects/foo/bar"

    def test_no_extension(self):
        result = vault_path_to_wiki_link("/vault", "/vault/foo.md")
        assert result == "foo"


class TestAddSeeAlso:
    def test_adds_section(self):
        content = "Some content"
        result = add_see_also(content, [("foo/bar", "Bar")])
        assert "## See Also" in result
        assert "[[foo/bar|Bar]]" in result

    def test_skips_if_already_exists(self):
        content = "Some content\n\n## See Also\n- existing"
        result = add_see_also(content, [("new/link", "New")])
        assert result == content

    def test_empty_links(self):
        content = "Some content"
        result = add_see_also(content, [])
        assert result == content


class TestStripFrontmatter:
    def test_strips_frontmatter(self):
        content = "---\ntags: [a]\n---\n\nBody text"
        result = strip_frontmatter(content)
        assert result == "Body text"

    def test_no_frontmatter(self):
        content = "Just body text"
        assert strip_frontmatter(content) == content

    def test_incomplete_frontmatter(self):
        content = "---\nunclosed"
        assert strip_frontmatter(content) == content
