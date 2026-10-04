"""One size budget per kind of post, enforced here rather than by the author.

Discord posts are read on a phone, in a channel that also carries a swarm's
noise, so the spec fixes a size for every kind of message and fixes the voice:
a leading glyph from a fixed set, plain prose, and **exactly one** link, last,
bare and wrapped in ``<>`` so Discord renders no preview card
(``2026-10-03-discord-as-a-chat-extension-of-the-supervisor.md`` §3.1).  §3.2
then gives each post its line and character budget.

The budgets are data here so a producer cannot opt out: it builds the body
lines it wants and calls :func:`compose`, which cuts them to the row's budget
and appends the one link.  Two rules make the cut predictable:

* body lines are ordered most important first, so overflow is taken from the
  end -- §3.2's "decision line kept, summary cut" is an ordering, not a special
  case, and a renderer that orders its lines the other way round gets the
  wrong line cut;
* the delivery marker and the link are never cut.  They are what makes a
  re-post idempotent and what makes the post actionable, so an origin long
  enough to exhaust the budget yields the envelope alone rather than a post
  whose link points nowhere.  That can exceed ``chars`` and it can never reach
  Discord's own 2000-character ceiling, which the transport enforces separately
  (a safety net, not the budget).

The link itself is built by :func:`link_line` from the origin the single
resolver returned (:mod:`src.remote_links`) and a path from
:mod:`src.dashboard_paths`, which owns the §6.1 scheme.  No renderer joins an
origin and a path by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.dashboard_paths import dashboard_href

#: Every cut ends with exactly one of these.
ELLIPSIS = "…"

#: §3.1's fixed set of leading glyphs.  A post that invents its own is a bug.
GLYPH_LANDED = "✅"
GLYPH_WORKING = "⏳"
GLYPH_STUCK = "⚠️"
GLYPH_NEEDS_YOU = "❓"
GLYPH_REVIEW = "📄"
GLYPH_DIGEST = "📊"
GLYPH_OFFLINE = "⏸"
#: §5.2's state table names two further glyphs for the escalation rows the
#: other seven do not cover: a post a human has already answered, and one that
#: no longer needs anyone.  They join the same fixed set rather than living
#: beside it, so "one glyph from the set" stays the invariant every post obeys.
GLYPH_ANSWERED = "💬"
GLYPH_OBSOLETE = "⚪"
GLYPHS = frozenset(
    {
        GLYPH_LANDED,
        GLYPH_WORKING,
        GLYPH_STUCK,
        GLYPH_NEEDS_YOU,
        GLYPH_REVIEW,
        GLYPH_DIGEST,
        GLYPH_OFFLINE,
        GLYPH_ANSWERED,
        GLYPH_OBSOLETE,
    }
)

#: Stand-in for a link that cannot be built, so a post is never silently linkless.
LINK_UNAVAILABLE = "The dashboard link could not be built for this post."


@dataclass(frozen=True, slots=True)
class PostBudget:
    """§3.2's row: how many body lines and characters a post may spend.

    ``body_lines`` of ``None`` means the row leaves the line count free (a chat
    reply is split into ``posts`` messages instead).  ``section_lines`` is the
    overflow rule for a post made of sections rather than one body: each
    section is cut to at most that many lines.
    """

    kind: str
    body_lines: int | None
    chars: int
    posts: int = 1
    section_lines: int = 1


#: §3.2, verbatim: kind -> budget.
REVIEW_READY = PostBudget("review_ready", 2, 300)
ESCALATION_OPEN = PostBudget("escalation_open", 2, 400)
ESCALATION_CLOSED = PostBudget("escalation_closed", 1, 160)
ESCALATION_THREAD = PostBudget("escalation_thread", 4, 600)
DIGEST = PostBudget("digest", 3, 600, section_lines=2)
CHAT_REPLY = PostBudget("chat_reply", None, 1900, posts=2)
STATUS_LINE = PostBudget("status_line", 1, 120)

BUDGETS: dict[str, PostBudget] = {
    budget.kind: budget
    for budget in (
        REVIEW_READY,
        ESCALATION_OPEN,
        ESCALATION_CLOSED,
        ESCALATION_THREAD,
        DIGEST,
        CHAT_REPLY,
        STATUS_LINE,
    )
}

#: §3.2 row 1's own field caps: the title stops at 120 and the revision's
#: ``changes_note`` at 160.  The second is the regression for the review posts
#: that Discord rejected with ``50035`` for exceeding 2000 characters.
REVIEW_TITLE_CHARS = 120
REVIEW_CHANGES_NOTE_CHARS = 160


def budget_for(kind: str) -> PostBudget:
    """The §3.2 row named ``kind``; an unknown kind is a bug, not a default."""
    try:
        return BUDGETS[kind]
    except KeyError:
        raise ValueError(f"no size budget for {kind!r}; known kinds: {sorted(BUDGETS)}") from None


def cut(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, ending in a single ellipsis."""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + ELLIPSIS


def clip_section(text: str, max_lines: int) -> str:
    """A section cut to ``max_lines`` lines (§3.2's "each section cut to 2 lines").

    Cut, not dropped: the reader learns that the section continues rather than
    that it is gone.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if max_lines <= 0:
        return ""
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join([*lines[:max_lines], ELLIPSIS])


def first_line(text: str) -> str:
    """``text`` as one line, so a post's line budget is never spent on wrapping."""
    return " ".join(line.strip() for line in (text or "").splitlines() if line.strip())


def link_line(base_url: str, path: str = "", *, notice: str = "") -> str:
    """The post's one link line: a bare URL in ``<>``, or why there is none.

    ``base_url`` is the origin :mod:`src.remote_links` resolved and ``path`` one
    of :mod:`src.dashboard_paths`' §6.1 paths; this is the only place a posted
    link is assembled.  An unconfigured origin renders the resolver's notice
    rather than a dead link, and an origin that cannot be carried as link
    markup degrades to the notice instead of breaking the post.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return (notice or "").strip()
    url = dashboard_href(base, path) if path else base
    if any(char.isspace() for char in url) or ">" in url or "<" in url:
        return notice.strip() or LINK_UNAVAILABLE
    return f"<{url}>"


def _joined(lines: Sequence[str], tail: Sequence[str]) -> str:
    return "\n".join(part for part in (*lines, *tail) if part)


def _size(lines: Sequence[str], tail: Sequence[str]) -> int:
    return len(_joined(lines, tail))


def compose(
    body: Sequence[str],
    budget: PostBudget,
    *,
    link: str = "",
    marker: str = "",
    chars: int | None = None,
) -> str:
    """Cut ``body`` to ``budget`` and append the marker and the single link.

    Body lines are most important first, so overflow is taken from the end and
    the last survivor is cut rather than dropped; ``marker`` and ``link`` are
    appended in that order, the link last, and neither is ever cut.  ``chars``
    tightens the budget for a caller that reserves room for text it appends
    itself (a delivery marker); it can never loosen the budget.
    """
    limit = budget.chars if chars is None else min(chars, budget.chars)
    lines = [first_line(line) for line in body]
    lines = [line for line in lines if line][: budget.body_lines or None]
    tail = [part.strip() for part in (marker, link) if part and part.strip()]
    while lines and _size(lines, tail) > limit:
        dropped = lines.pop()
        if lines:
            continue
        # The last survivor is cut to what the envelope leaves, and the newline
        # that separates it from the envelope is part of what it must leave.
        # An envelope that leaves nothing yields the envelope alone: a link is
        # never cut into one that points somewhere else.
        room = limit - _size([], tail) - (1 if tail else 0)
        survivor = cut(dropped, room)
        lines = [survivor] if survivor else []
        break
    return _joined(lines, tail)


def split_posts(text: str, budget: PostBudget, *, link: str = "") -> list[str]:
    """At most ``budget.posts`` messages for one reply that does not fit.

    The last one carries ``…`` and the conversation link, so a split reply
    always says where the rest of the answer is.
    """
    remaining = " ".join((text or "").split())
    if not remaining:
        return [link] if link else []
    if len(remaining) <= budget.chars:
        return [remaining]
    tail = " ".join(part for part in (ELLIPSIS, link) if part)
    if len(tail) > budget.chars:
        raise ValueError("conversation link exceeds the post budget")
    posts: list[str] = []
    for index in range(budget.posts):
        last = index == budget.posts - 1 or len(remaining) <= budget.chars
        room = max(budget.chars - len(tail) - 1, 0) if last else budget.chars
        boundary = min(len(remaining), room)
        if boundary < len(remaining):
            separator = remaining.rfind(" ", 0, boundary + 1)
            if separator > 0:
                boundary = separator
        chunk = remaining[:boundary].rstrip()
        if last:
            posts.append(" ".join(part for part in (chunk, tail) if part))
            break
        posts.append(chunk)
        remaining = remaining[boundary:].lstrip()
    return posts


def status_line(text: str, budget: PostBudget = STATUS_LINE, *, glyph: str = GLYPH_OFFLINE) -> str:
    """One line within ``budget.chars`` -- §3.2's offline / status row.

    The glyph is prepended unless the caller already led with one, so a status
    post is recognisable in the channel the way every other post is.
    """
    line = first_line(text)
    if line and line[0] not in GLYPHS:
        line = f"{glyph} {line}"
    return compose([line], budget)


def review_ready_lines(
    *,
    revision: int,
    kind: str,
    title: str,
    changes_note: str = "",
    glyph: str = GLYPH_REVIEW,
) -> list[str]:
    """§3.2 row 1: the heading (title cut at 120) and the note (cut at 160).

    The review id appears once, as the link, so it is not part of the body; the
    note is what a long revision spends its room on, and cutting it here is
    what keeps the post under Discord's ceiling.
    """
    heading = (
        f"{glyph} {kind} for review: {cut(title, REVIEW_TITLE_CHARS)}"
        if revision == 1
        else f"{glyph} Revised {kind} (rev {revision}): {cut(title, REVIEW_TITLE_CHARS)}"
    )
    lines = [heading]
    note = cut(changes_note, REVIEW_CHANGES_NOTE_CHARS)
    if note:
        lines.append(note)
    return lines


def review_ready_post(
    *,
    revision: int,
    kind: str,
    title: str,
    changes_note: str = "",
    base_url: str = "",
    link_path: str = "",
    unavailable_notice: str = "",
    marker: str = "",
) -> str:
    """The whole review-ready post, inside :data:`REVIEW_READY`'s budget.

    This is the review row's renderer: a producer that announces a revision
    hands it the fields and gets the post, already cut to 300 characters with
    the review's page as its one link.  The transport keeps its own separate
    bound below Discord's 2000-character ceiling as a safety net for whatever
    reaches it; this is the post's actual size.
    """
    return compose(
        review_ready_lines(revision=revision, kind=kind, title=title, changes_note=changes_note),
        REVIEW_READY,
        link=link_line(base_url, link_path, notice=unavailable_notice),
        marker=marker,
    )
