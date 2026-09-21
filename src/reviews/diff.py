"""Block-level diff between two revisions of a markdown document (spec §8.2).

The dashboard has no diff library, so the daemon compares revisions here and
the review pane highlights whole blocks.  A block is a run of lines between
blank lines, except that a fenced code block is always one block, blank lines
and all: the pane renders each block as markdown on its own, and half a fence
does not render.
"""

from __future__ import annotations

import difflib
import re

#: An opening or closing code fence: up to three spaces, then three or more
#: backticks or tildes (CommonMark §4.5).
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None  # the opening fence's run, while inside one
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _FENCE.match(line)
        if fence is None:
            if not line.strip():
                if current:
                    blocks.append("\n".join(current))
                    current = []
                continue
            if match:
                fence = match.group(1)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            if not line[match.end() :].strip():
                fence = None
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def block_diff(old: str, new: str) -> list[dict]:
    """``[{"op": "equal"|"added"|"removed", "text": block}]`` in document order.

    A changed block reads as the old block ``removed`` followed by the new
    one ``added``; ``equal`` blocks carry the new revision's text.
    """
    a, b = _blocks(old), _blocks(new)
    out: list[dict] = []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            out.extend({"op": "equal", "text": t} for t in b[j1:j2])
            continue
        out.extend({"op": "removed", "text": t} for t in a[i1:i2])
        out.extend({"op": "added", "text": t} for t in b[j1:j2])
    return out
