"""Vault file helpers for document reviews (spec §3.1, §7).

The review's vault file is a copy of the current revision that the daemon
writes after each database commit: a YAML frontmatter block carrying the
review's state, then the revision body.  Divergence (spec §7) compares the
body alone, so these helpers split and render the two halves and hash the
body the same way the database stores it.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterator
from pathlib import Path

from ruamel.yaml import YAML

_KIND_DIRS = {"spec": "specs", "plan": "plans", "other": "specs"}

#: A leading ``---`` line, the YAML, then the first line that is exactly
#: ``---``.  Fences must be whole lines, so a ``----`` rule inside the block
#: does not end it; ``\r\n`` fences are accepted from pasted documents.
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(?:(?P<yaml>.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)

_MAX_SLUG = 60


def slugify(title: str) -> str:
    """Lowercase ASCII words joined by ``-``; accents are folded, not dropped."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:_MAX_SLUG].rstrip("-") or "document"


def candidate_paths(project_id: str, kind: str, title: str, date: str) -> Iterator[str]:
    """Vault-relative paths for a new document, first choice then ``-2``, ``-3``…"""
    base = f"projects/{project_id}/{_KIND_DIRS[kind]}/{date}-{slugify(title)}"
    yield f"{base}.md"
    n = 2
    while True:
        yield f"{base}-{n}.md"
        n += 1


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """``(frontmatter, body)``; ``frontmatter`` is ``None`` when there is none."""
    match = _FRONTMATTER.match(text)
    if not match:
        return None, text
    return match.group("yaml") or "", text[match.end() :]


def body_sha256(body: str) -> str:
    """The ``content_sha256`` of a revision body: sha256 of its UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render(frontmatter: dict, body: str) -> str:
    """A vault file: *frontmatter* as a YAML block, then *body* unchanged."""
    yaml = YAML(typ="rt")
    yaml.width = 4096  # one line per key, however long the title
    buf = io.StringIO()
    yaml.dump(frontmatter, buf)
    return f"---\n{buf.getvalue().rstrip()}\n---\n{body}"


def write_atomic(path: Path, text: str) -> None:
    """Replace *path* with *text* in one rename, creating parent directories.

    A reader (Obsidian, the divergence check) sees the old file or the new
    one, never a partial write.  The file keeps its existing permissions, or
    gets ``0644`` when new (``mkstemp`` would otherwise leave it ``0600``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        # ``newline=""``: the file holds exactly *text*; the body hash (§7)
        # is taken over the bytes a reader gets back.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
