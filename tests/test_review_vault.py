"""Document review: vault file helpers (spec §3.1, §7; plan Task 2)."""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

from src.reviews.vault import (
    body_sha256,
    candidate_paths,
    render,
    slugify,
    split_frontmatter,
    write_atomic,
)


def test_slugify_and_candidates():
    assert slugify("Document review: in the Dashboard!") == "document-review-in-the-dashboard"
    assert slugify("!!!") == "document"
    paths = candidate_paths("agent-queue", "plan", "X", "2026-09-21")
    assert next(paths) == "projects/agent-queue/plans/2026-09-21-x.md"
    assert next(paths) == "projects/agent-queue/plans/2026-09-21-x-2.md"
    assert next(paths) == "projects/agent-queue/plans/2026-09-21-x-3.md"
    assert next(candidate_paths("p", "other", "X", "2026-09-21")).startswith("projects/p/specs/")
    assert next(candidate_paths("p", "spec", "X", "2026-09-21")).startswith("projects/p/specs/")


def test_slugify_folds_accents_and_bounds_length():
    assert slugify("Überblick über Café") == "uberblick-uber-cafe"
    long = slugify("word " * 40)
    assert len(long) <= 60
    assert not long.endswith("-")


def test_render_split_roundtrip():
    text = render({"title": "T", "status": "in_review", "revision": 2}, "# T\n\nBody.\n")
    fm, body = split_frontmatter(text)
    assert "status: in_review" in fm
    assert body == "# T\n\nBody.\n"
    assert split_frontmatter("# no frontmatter\n") == (None, "# no frontmatter\n")


def test_render_writes_a_date_unquoted_and_a_long_title_on_one_line():
    title = " ".join(["A title"] * 30)
    text = render({"title": title, "date": datetime.date(2026, 9, 21)}, "body\n")
    assert "\ndate: 2026-09-21\n" in text
    assert f"\ntitle: {title}\n" in text
    fm, body = split_frontmatter(text)
    assert body == "body\n"
    assert len(fm.splitlines()) == 2


def test_split_needs_a_whole_closing_fence_line():
    # A horizontal rule of four dashes is not the end of the frontmatter.
    text = "---\na: 1\n----\nb: 2\n---\nbody\n"
    fm, body = split_frontmatter(text)
    assert fm == "a: 1\n----\nb: 2"
    assert body == "body\n"
    # An empty frontmatter block and a file that is only frontmatter.
    assert split_frontmatter("---\n---\n# x\n") == ("", "# x\n")
    assert split_frontmatter("---\na: 1\n---") == ("a: 1", "")
    # An unterminated block is not frontmatter.
    assert split_frontmatter("---\na: 1\n") == (None, "---\na: 1\n")


def test_split_accepts_crlf_fences():
    fm, body = split_frontmatter("---\r\ntitle: x\r\n---\r\n# Body\r\n")
    assert fm == "title: x"
    assert body == "# Body\r\n"


def test_split_keeps_a_second_frontmatter_like_block_in_the_body():
    body = "---\nnot: frontmatter\n---\n# Heading\n"
    fm, rest = split_frontmatter(render({"title": "T"}, body))
    assert fm == "title: T"
    assert rest == body


def test_body_sha256_is_stable():
    assert body_sha256("a\n") == body_sha256("a\n")
    assert body_sha256("a\n") != body_sha256("b\n")
    assert body_sha256("é") == hashlib.sha256("é".encode()).hexdigest()


def test_write_atomic_creates_parents(tmp_path: Path):
    target = tmp_path / "projects" / "p" / "specs" / "x.md"
    write_atomic(target, "hello")
    assert target.read_text() == "hello"
    assert not list(target.parent.glob("*.tmp"))


def test_write_atomic_replaces_and_keeps_a_readable_mode(tmp_path: Path):
    target = tmp_path / "x.md"
    write_atomic(target, "one")
    assert target.stat().st_mode & 0o777 == 0o644
    os.chmod(target, 0o664)
    write_atomic(target, "two — ü")
    assert target.read_text(encoding="utf-8") == "two — ü"
    assert target.stat().st_mode & 0o777 == 0o664
    assert [p.name for p in tmp_path.iterdir()] == ["x.md"]
