"""Document review: block diff between revisions (spec §8.2; plan Task 2)."""

from __future__ import annotations

from src.reviews.diff import block_diff


def _ops(old: str, new: str) -> list[tuple[str, str]]:
    return [(d["op"], d["text"]) for d in block_diff(old, new)]


def test_block_diff_marks_changed_blocks():
    old = "# T\n\nkeep\n\nold para\n"
    new = "# T\n\nkeep\n\nnew para\n\nadded\n"
    assert _ops(old, new) == [
        ("equal", "# T"),
        ("equal", "keep"),
        ("removed", "old para"),
        ("added", "new para"),
        ("added", "added"),
    ]


def test_identical_and_empty_documents():
    assert _ops("a\n\nb\n", "a\n\nb\n") == [("equal", "a"), ("equal", "b")]
    assert _ops("", "") == []
    assert _ops("", "a\n") == [("added", "a")]
    assert _ops("a\n", "") == [("removed", "a")]


def test_blank_line_runs_and_crlf_do_not_create_blocks():
    old = "a\r\n\r\n\r\n   \r\nb\r\n"
    new = "a\n\nb\n"
    assert _ops(old, new) == [("equal", "a"), ("equal", "b")]


def test_a_fenced_code_block_with_blank_lines_is_one_block():
    fence = "```python\ndef f():\n\n    return 1\n```"
    old = f"intro\n\n{fence}\n\nend\n"
    new = f"intro\n\n{fence.replace('1', '2')}\n\nend\n"
    assert _ops(old, new) == [
        ("equal", "intro"),
        ("removed", fence),
        ("added", fence.replace("1", "2")),
        ("equal", "end"),
    ]


def test_a_tilde_fence_closes_only_on_a_matching_fence():
    block = "~~~~\n```\n\nstill code\n~~~\n\nstill code\n~~~~"
    assert _ops("", f"{block}\n\nafter\n") == [("added", block), ("added", "after")]


def test_an_unclosed_fence_runs_to_the_end():
    block = "```\ncode\n\nmore"
    assert _ops("", block) == [("added", block)]
