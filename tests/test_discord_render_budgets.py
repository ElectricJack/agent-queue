"""Every row of the §3.2 size budget, pinned by the renderer that spends it.

The rule the whole module exists for: a producer cannot opt out of a budget.
It builds the lines it wants and
:func:`~src.discord.render_budget.compose` cuts them to the row -- line count,
character count, one link last -- so no fact field, no long revision note and
no supervisor ramble can grow a post past what a phone shows.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.dashboard_paths import conversation_path, escalation_path, review_path
from src.digest import DigestInputs, DigestWindow, WorkFact, build_digest
from src.digest.facts import KIND_COMPLETED, KIND_PROGRESS, KIND_STARTED
from src.discord.render_budget import (
    BUDGETS,
    CHAT_REPLY,
    DIGEST,
    ELLIPSIS,
    ESCALATION_CLOSED,
    ESCALATION_OPEN,
    ESCALATION_THREAD,
    GLYPH_LANDED,
    GLYPH_OFFLINE,
    GLYPH_WORKING,
    GLYPHS,
    REVIEW_CHANGES_NOTE_CHARS,
    REVIEW_READY,
    REVIEW_TITLE_CHARS,
    STATUS_LINE,
    budget_for,
    clip_section,
    compose,
    cut,
    link_line,
    review_ready_post,
    split_posts,
    status_line,
)
from src.escalations import (
    EscalationFacts,
    MentionPolicy,
    render_resolution,
    render_resolved_root,
    render_root,
    render_thread_opener,
)
from src.remote_links import normalise_public_origin

ORIGIN = "https://aq.example.ts.net"
NOW = 1_000_000.0
WINDOW = DigestWindow(since=NOW - 3600, until=NOW)


def facts(**overrides) -> EscalationFacts:
    values = {
        "id": "esc-1",
        "project_id": "agent-queue",
        "state": "needs_human",
        "revision": 0,
        "severity": "high",
        "summary": "The replica is behind and the migration cannot apply",
        "investigation": "Reproduced on staging; the replica is three revisions behind",
        "decision_requested": "Roll the replica forward or hold the release?",
        "task_id": "nimble-torrent-66",
        "task_title": "Deploy the migration",
        "choices": ("roll forward", "hold"),
    }
    values.update(overrides)
    return EscalationFacts(**values)


def work_fact(key, *, kind=KIND_COMPLETED, detail="did the thing", task_id="t1"):
    return WorkFact(
        key=key,
        kind=kind,
        category="work",
        project_id="p",
        task_id=task_id,
        title=f"Task {task_id}",
        at=NOW - 60,
        detail=detail,
    )


def digest_text(facts=None, **kwargs) -> str:
    dashboard_kwargs = {"dashboard_url": ORIGIN}
    inputs_kwargs = {}
    for name in ("dashboard_url", "dashboard_notice", "max_chars", "max_highlights"):
        if name in kwargs:
            dashboard_kwargs[name] = kwargs.pop(name)
    inputs_kwargs.update(kwargs)
    return build_digest(
        DigestInputs(window=WINDOW, facts=facts or (work_fact("c1"),), **inputs_kwargs),
        **dashboard_kwargs,
    ).text


def body_lines(post: str, *, envelope: int) -> list[str]:
    """``post``'s body lines: everything the renderer appended is excluded."""
    lines = post.splitlines()
    return lines[: len(lines) - envelope]


# ------------------------------------------------------------------ the table


def test_every_row_of_the_table_is_a_budget_and_nothing_else_is():
    assert set(BUDGETS) == {
        "review_ready",
        "escalation_open",
        "escalation_closed",
        "escalation_thread",
        "digest",
        "chat_reply",
        "status_line",
    }


@pytest.mark.parametrize(
    ("kind", "body_lines", "chars", "posts"),
    [
        ("review_ready", 2, 300, 1),
        ("escalation_open", 2, 400, 1),
        ("escalation_closed", 1, 160, 1),
        ("escalation_thread", 4, 600, 1),
        ("digest", 3, 600, 1),
        ("chat_reply", None, 1900, 2),
        ("status_line", 1, 120, 1),
    ],
)
def test_each_row_is_the_size_the_spec_gives_it(kind, body_lines, chars, posts):
    budget = budget_for(kind)
    assert (budget.body_lines, budget.chars, budget.posts) == (body_lines, chars, posts)


def test_an_unknown_kind_is_a_bug_rather_than_a_generous_default():
    with pytest.raises(ValueError, match="no size budget"):
        budget_for("escalation_root")


# ------------------------------------------------- row 1: review ready (2 + 300)


def review_post(**kwargs) -> str:
    kwargs.setdefault("revision", 1)
    kwargs.setdefault("kind", "spec")
    kwargs.setdefault("title", "Discord as a chat extension of the supervisor")
    kwargs.setdefault("base_url", ORIGIN)
    kwargs.setdefault("link_path", review_path("rev-1"))
    return review_ready_post(**kwargs)


def test_a_review_post_is_two_lines_and_a_link():
    post = review_post(changes_note="Answers the threading and quiet-hours questions.")
    assert len(body_lines(post, envelope=1)) == 2
    assert len(post) <= REVIEW_READY.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/reviews/rev-1>"


def test_the_revision_changes_note_is_capped_at_160_characters():
    """The regression for the posts Discord rejected with ``50035``."""
    post = review_post(revision=2, changes_note="x" * 4000)
    assert len(post) <= REVIEW_READY.chars
    note = body_lines(post, envelope=1)[-1]
    assert len(note) == REVIEW_CHANGES_NOTE_CHARS
    assert note.endswith(ELLIPSIS)
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/reviews/rev-1>"


def test_a_review_title_is_cut_at_120_characters():
    post = review_post(title="y" * 4000)
    heading = body_lines(post, envelope=1)[0]
    assert len(heading) <= REVIEW_TITLE_CHARS + len("📄 spec for review: ")
    assert heading.endswith(ELLIPSIS)


def test_a_review_post_with_no_note_is_one_line_and_still_links():
    post = review_post()
    assert len(body_lines(post, envelope=1)) == 1
    assert len(post) <= REVIEW_READY.chars
    assert post.splitlines()[-1].startswith("<")


# -------------------------------------------- row 2: escalation root (2 + 400)


def test_an_open_escalation_is_two_lines_400_characters_and_one_link():
    post = render_root(facts(), mentions=MentionPolicy(), base_url=ORIGIN, dedup_key="esc-1:root:0")
    assert len(body_lines(post, envelope=2)) == 2  # marker line + link
    assert len(post) <= ESCALATION_OPEN.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/escalations/esc-1>"


def test_the_decision_line_is_kept_and_the_summary_is_what_gives_way():
    post = render_root(
        facts(decision_requested="Keep the Opus trial or revert to Sonnet?", summary="z" * 4000),
        mentions=MentionPolicy(),
        base_url=ORIGIN,
        dedup_key="esc-1:root:0",
    )
    assert "Keep the Opus trial or revert to Sonnet?" in post
    summary_line = body_lines(post, envelope=2)[-1]
    assert summary_line.endswith(ELLIPSIS)
    assert len(post) <= ESCALATION_OPEN.chars


def test_a_summary_too_long_for_the_post_at_all_is_dropped_not_the_decision():
    post = render_root(
        facts(decision_requested="d" * 4000, summary="z" * 4000),
        mentions=MentionPolicy(),
        base_url=ORIGIN,
        dedup_key="esc-1:root:0",
    )
    assert "z" not in post.splitlines()[0]
    assert len(post) <= ESCALATION_OPEN.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/escalations/esc-1>"


def test_every_field_the_facts_carry_still_fits_the_open_root_budget():
    post = render_root(
        facts(
            summary="s" * 4000,
            investigation="i" * 4000,
            decision_requested="d" * 4000,
            task_title="t" * 4000,
            task_id="k" * 4000,
            project_id="p" * 4000,
            choices=tuple(f"choice {index}" for index in range(50)),
        ),
        mentions=MentionPolicy(user_ids=("111111111111111111",), role_ids=("222222222222222222",)),
        base_url=ORIGIN,
        dedup_key="esc-1:root:0",
    )
    assert len(post) <= ESCALATION_OPEN.chars
    assert len(body_lines(post, envelope=2)) <= ESCALATION_OPEN.body_lines
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/escalations/esc-1>"


def test_the_configured_mention_survives_the_cut_wherever_the_rollover_lands():
    post = render_root(
        facts(decision_requested="d" * 4000),
        mentions=MentionPolicy(user_ids=("111111111111111111",)),
        base_url=ORIGIN,
        dedup_key="esc-1:root:0",
    )
    assert "<@111111111111111111>" in post
    assert post.splitlines()[0].count("<@") == 1


# ------------------------------------- row 3: escalation root closed (1 + 160)


@pytest.mark.parametrize("state", ["resolved", "cancelled", "stale"])
def test_a_closed_escalation_root_is_one_line_and_160_characters(state):
    post = render_resolved_root(
        facts(state=state, revision=3, terminal_outcome="Rolled the replica forward"),
        dedup_key="esc-1:resolution:0:root",
    )
    assert len(body_lines(post, envelope=1)) == 1
    assert len(post) <= ESCALATION_CLOSED.chars


def test_a_closed_escalation_root_cuts_a_long_outcome_rather_than_the_line():
    post = render_resolved_root(
        facts(state="resolved", revision=3, terminal_outcome="Rolled it forward. " * 40),
        dedup_key="esc-1:resolution:0:root",
    )
    assert len(post) <= ESCALATION_CLOSED.chars
    assert "Resolved" in post


def test_a_closed_escalation_root_says_so_with_a_real_incident_id():
    """``escalation-<uuid>`` is the production id; the collapsed row still speaks."""
    post = render_resolved_root(
        facts(
            id=f"escalation-{uuid4()}",
            state="resolved",
            revision=3,
            terminal_outcome="Rolled the replica forward",
        ),
        dedup_key="esc-1:resolution:0:root",
    )
    assert len(post) <= ESCALATION_CLOSED.chars
    assert post.startswith(f"{GLYPH_LANDED} Resolved: Rolled the replica forward")


# ------------------------------------- row 4: escalation thread reply (4 + 600)


def test_a_thread_reply_is_at_most_four_lines_and_600_characters():
    post = render_thread_opener(
        facts(
            investigation="i" * 4000,
            decision_requested="d" * 4000,
            choices=tuple(f"choice {index} " + "x" * 200 for index in range(20)),
        ),
        base_url=ORIGIN,
        dedup_key="esc-1:thread:0",
    )
    assert len(body_lines(post, envelope=2)) <= ESCALATION_THREAD.body_lines
    assert len(post) <= ESCALATION_THREAD.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/escalations/esc-1>"


def test_a_resolution_reply_is_inside_the_thread_budget_too():
    post = render_resolution(
        facts(state="resolved", revision=3, terminal_outcome="Rolled it forward. " * 60),
        base_url=ORIGIN,
        dedup_key="esc-1:resolution:1:thread",
    )
    assert len(post) <= ESCALATION_THREAD.chars
    assert "Resolved" in post


# --------------------------------------------------- row 5: digest (3 + 600)


def test_a_digest_is_three_lines_600_characters_and_one_link():
    post = digest_text(tuple(work_fact(f"c{index}") for index in range(20)))
    assert len(body_lines(post, envelope=1)) <= DIGEST.body_lines
    assert len(post) <= DIGEST.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/inbox>"


def test_each_digest_section_is_cut_to_two_lines():
    post = digest_text((work_fact("c1", detail="a\nb\nc\nd\n"),))
    sections = body_lines(post, envelope=1)[1:]
    assert sections
    assert all(len(section.splitlines()) <= DIGEST.section_lines for section in sections)


def test_a_digest_never_grows_past_its_budget_however_long_the_window():
    post = digest_text(
        tuple(
            work_fact(f"c{index}", detail=f"finished chunk {index} " + "x" * 400)
            for index in range(200)
        ),
        open_escalations=9,
    )
    assert len(post) <= DIGEST.chars
    assert post.splitlines()[-1] == f"<{ORIGIN}/focus/inbox>"


def test_a_digest_reports_what_it_left_out():
    post = digest_text(tuple(work_fact(f"c{index}", task_id=f"t{index}") for index in range(9)))
    assert "more" in post


def test_a_digest_splits_across_no_posts():
    post = digest_text((work_fact("c1", detail="x" * 2000),))
    assert "\n\n" not in post
    assert len(post.splitlines()) <= DIGEST.body_lines + 1


# ------------------------------------------- row 6: chat reply (1900, 2 posts)


def test_a_chat_reply_that_fits_is_one_post():
    assert split_posts("a short answer", CHAT_REPLY, link="") == ["a short answer"]


def test_a_chat_reply_over_budget_becomes_two_posts_with_an_ellipsis_and_the_link():
    link = link_line(ORIGIN, conversation_path("conv-1"))
    posts = split_posts("word " * 2000, CHAT_REPLY, link=link)
    assert len(posts) == CHAT_REPLY.posts == 2
    assert all(len(post) <= CHAT_REPLY.chars for post in posts)
    assert posts[-1].endswith(f"{ELLIPSIS} <{ORIGIN}/focus/conversations/conv-1>")


@pytest.mark.parametrize("reply", ["x" * 5000, "short " * 300 + "x" * 2500])
def test_a_chat_reply_with_an_unbroken_token_stays_within_each_post_budget(reply):
    link = link_line(ORIGIN, conversation_path("conv-1"))
    posts = split_posts(reply, CHAT_REPLY, link=link)
    assert len(posts) == CHAT_REPLY.posts
    assert all(0 < len(post) <= CHAT_REPLY.chars for post in posts)
    assert posts[-1].endswith(f"{ELLIPSIS} {link}")


def test_a_chat_reply_that_fits_in_two_posts_keeps_the_final_dashboard_pointer():
    link = link_line(ORIGIN, conversation_path("conv-1"))
    posts = split_posts("word " * 400, CHAT_REPLY, link=link)
    assert len(posts) == CHAT_REPLY.posts
    assert all(len(post) <= CHAT_REPLY.chars for post in posts)
    assert posts[-1].endswith(f"{ELLIPSIS} {link}")
    assert " ".join(posts).removesuffix(f" {ELLIPSIS} {link}") == " ".join(["word"] * 400)


def test_a_chat_reply_that_needs_no_split_carries_no_ellipsis():
    posts = split_posts("word " * 100, CHAT_REPLY, link="")
    assert len(posts) == 1
    assert ELLIPSIS not in posts[0]


def test_an_empty_chat_reply_is_the_link_or_nothing():
    assert split_posts("", CHAT_REPLY, link="<l>") == ["<l>"]
    assert split_posts("", CHAT_REPLY, link="") == []


# ------------------------------------------- row 7: offline / status line (1 + 120)


def test_a_status_line_is_one_line_and_120_characters():
    assert status_line("offline") == f"{GLYPH_OFFLINE} offline"
    line = status_line("offline\nthe daemon is not answering " + "x" * 400)
    assert len(line) <= STATUS_LINE.chars
    assert "\n" not in line
    assert line.endswith(ELLIPSIS)


def test_a_status_line_keeps_the_glyph_its_caller_chose():
    assert status_line(f"{GLYPH_WORKING} working on it") == f"{GLYPH_WORKING} working on it"


# ------------------------------------------------------------- the mechanism


def test_the_conversation_reply_limit_is_the_same_row_and_cannot_drift():
    """The conversation outbox bounds its own reply; §3.2 says the same number."""
    from src.conversations.limits import MAX_REPLY_CHARS

    assert MAX_REPLY_CHARS == CHAT_REPLY.chars


def test_overflow_is_taken_from_the_end_so_the_first_line_survives():
    post = compose(["keep me", "and me", "drop me first", "drop me too"], REVIEW_READY)
    assert post == "keep me\nand me"


def test_a_single_line_that_does_not_fit_is_cut_not_dropped():
    post = compose(["x" * 400], REVIEW_READY)
    assert len(post) == REVIEW_READY.chars
    assert post.endswith(ELLIPSIS)


def test_the_link_and_the_marker_are_never_cut():
    marker = "-# aq-esc:esc-1:root:0"
    link = f"<{ORIGIN}/focus/escalations/esc-1>"
    post = compose(["x" * 4000], ESCALATION_OPEN, link=link, marker=marker)
    assert post.splitlines()[-2:] == [marker, link]
    assert len(post) <= ESCALATION_OPEN.chars


def test_an_envelope_longer_than_the_budget_is_still_sent_whole():
    """A link that cannot fit must never be cut into one that points elsewhere."""
    link = f"<{ORIGIN}/{'x' * ESCALATION_CLOSED.chars}>"
    post = compose(["body"], ESCALATION_CLOSED, link=link)
    assert post == link
    assert len(post) > ESCALATION_CLOSED.chars


def test_a_post_with_no_body_is_its_envelope():
    assert compose([], REVIEW_READY, link="<l>") == "<l>"
    assert compose(["", "   "], REVIEW_READY, link="<l>") == "<l>"


def test_a_wrapped_body_line_is_flattened_before_it_is_counted():
    assert compose(["one\ntwo\nthree"], STATUS_LINE) == "one two three"


def test_a_callers_char_budget_tightens_the_row_but_never_loosens_it():
    budget = BUDGETS["review_ready"]
    assert len(compose(["x" * 400], budget, chars=100)) == 100
    assert len(compose(["x" * 400], budget, chars=4000)) == budget.chars


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 10, "short"),
        ("exactlyten", 10, "exactlyten"),
        ("elevenchars", 10, "elevencha" + ELLIPSIS),
        ("anything", 0, ""),
        ("anything", -5, ""),
        ("", 10, ""),
    ],
)
def test_cut_never_exceeds_its_limit(text, limit, expected):
    assert cut(text, limit) == expected
    assert len(cut(text, limit)) <= max(limit, 0)


def test_clip_section_cuts_to_its_line_budget_and_says_it_did():
    assert clip_section("one\ntwo", 2) == "one\ntwo"
    assert clip_section("one\ntwo\nthree", 2) == f"one\ntwo\n{ELLIPSIS}"
    assert clip_section("one", 0) == ""


def test_every_channel_post_leads_with_one_of_the_fixed_glyphs():
    """The glyph is what the reader scans the channel for, so it is not optional."""
    posts = (
        render_root(facts(), mentions=MentionPolicy(), base_url=ORIGIN, dedup_key="esc-1:root:0"),
        render_resolved_root(
            facts(state="resolved", terminal_outcome="Rolled it forward"), dedup_key="esc-1:res:0"
        ),
        review_post(),
        digest_text(),
        status_line("offline"),
    )
    for post in posts:
        assert post[0] in GLYPHS, post


def test_a_thread_reply_is_not_a_channel_post_and_needs_no_glyph():
    """§3.1's glyph marks the post a reader scans for; a thread reply is not one."""
    opener = render_thread_opener(facts(), base_url=ORIGIN, dedup_key="esc-1:thread:0")
    assert opener.startswith("This thread is where")


def test_an_escape_path_is_quoted_rather_than_reaching_the_resolver():
    from src.dashboard_paths import dashboard_href

    assert dashboard_href(ORIGIN, escalation_path("esc 1/../settings")) == (
        f"{ORIGIN}/focus/escalations/esc%201%2F..%2Fsettings"
    )


def test_the_digest_link_joins_the_resolver_origin_and_the_inbox_path():
    """The resolver hands over an origin and nothing else, so the join is exact."""
    origin, why = normalise_public_origin("https://aq.example.ts.net/")
    assert (origin, why) == (ORIGIN, "")
    assert digest_text(dashboard_url=origin).splitlines()[-1] == f"<{ORIGIN}/focus/inbox>"


def test_the_digest_keeps_its_counts_when_the_highlights_do_not_fit():
    post = digest_text(
        tuple(work_fact(f"c{index}", task_id=f"t{index}", detail="x" * 300) for index in range(9))
    )
    assert "9 completed" in post
    assert "active" in post
    assert len(post) <= DIGEST.chars


def test_started_and_progress_facts_still_reach_the_counts_line():
    post = digest_text(
        (
            work_fact("s1", kind=KIND_STARTED, task_id="t1"),
            work_fact("p1", kind=KIND_PROGRESS, task_id="t2"),
        )
    )
    assert "1 started" in post and "1 progressed" in post
    assert len(post) <= DIGEST.chars
