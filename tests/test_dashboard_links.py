"""One link resolver, one path scheme, one link per post (spec §6.1, §6.2).

The scheme table is pinned here so a renderer cannot invent a route, so no
post can name a ``/settings/`` page, and so every post that links anywhere
links exactly once, last, bare and wrapped in ``<>``.
"""

from __future__ import annotations

import pytest

from src.dashboard_paths import (
    SCHEME,
    batch_path,
    conversation_path,
    dashboard_href,
    escalation_path,
    inbox_path,
    review_path,
    session_path,
    task_path,
)
from src.digest import DigestInputs, DigestWindow, WorkFact, build_digest
from src.digest.facts import KIND_COMPLETED
from src.discord.render_budget import (
    STATUS_LINE,
    compose,
    link_line,
    review_ready_post,
    status_line,
)
from src.escalations import (
    EscalationFacts,
    MentionPolicy,
    render_ack,
    render_relay,
    render_resolution,
    render_resolved_root,
    render_root,
    render_thread_opener,
)
from src.remote_links import DashboardLink, DashboardLinkResolver

ORIGIN = "https://aq.example.ts.net"
NOW = 1_000_000.0


# ---------------------------------------------------------------- §6.1 scheme


def test_the_scheme_table_names_every_object_the_design_spec_lists():
    assert set(SCHEME) == {
        "task",
        "session",
        "report",
        "review",
        "escalation",
        "batch",
        "inbox",
        "conversation",
    }


@pytest.mark.parametrize(
    ("name", "oid", "expected_focus", "expected_desktop"),
    [
        ("task", "t-1", "/focus/tasks/t-1", "/tasks/{id}"),
        ("session", "s-1", "/focus/sessions/s-1", "/sessions/{id}"),
        ("report", "r-1", "/focus/reports/r-1", "/reports/{id}"),
        ("review", "rev-1", "/focus/reviews/rev-1", "/reviews/{id}"),
        # §6.1: an escalation has no desktop page, so both columns agree.
        ("escalation", "esc-1", "/focus/escalations/esc-1", "/focus/escalations/{id}"),
        ("batch", "b-1", "/focus/batches/b-1", "/projects/{project}/graph?batch={id}"),
        ("inbox", "", "/focus/inbox", "/reviews"),
        ("conversation", "c-1", "/focus/conversations/c-1", "/conversations"),
    ],
)
def test_each_object_opens_its_phone_first_page(name, oid, expected_focus, expected_desktop):
    target = SCHEME[name]
    assert target.focus(oid) == expected_focus
    assert target.desktop == expected_desktop


def test_the_table_cannot_claim_a_route_exists_when_it_does_not():
    assert SCHEME["task"].status == "live"
    assert SCHEME["session"].status == "live"
    assert SCHEME["report"].status == "live"
    assert {SCHEME[name].status for name in SCHEME} == {"live", "planned"}


def test_ids_are_quoted_so_a_path_cannot_escape_its_segment():
    assert task_path("a/b c") == "/focus/tasks/a%2Fb%20c"
    assert escalation_path("esc/1?x=2") == "/focus/escalations/esc%2F1%3Fx%3D2"
    assert review_path("rev 1") == "/focus/reviews/rev%201"
    assert session_path("../reports") == "/focus/sessions/..%2Freports"
    assert conversation_path("c/1") == "/focus/conversations/c%2F1"
    assert batch_path("b/1") == "/focus/batches/b%2F1"


def test_href_is_the_only_join_and_tolerates_a_trailing_slash():
    assert dashboard_href(ORIGIN, escalation_path("esc-1")) == f"{ORIGIN}/focus/escalations/esc-1"
    assert dashboard_href(f"{ORIGIN}/", "/focus/inbox") == f"{ORIGIN}/focus/inbox"
    assert inbox_path() == "/focus/inbox"


# --------------------------------------------------------------- the producers


def facts(**overrides) -> EscalationFacts:
    values = {
        "id": "esc-1",
        "project_id": "agent-queue",
        "state": "needs_human",
        "revision": 0,
        "severity": "high",
        "summary": "The replica is behind and the migration cannot apply",
        "investigation": "Reproduced on staging",
        "decision_requested": "Roll the replica forward or hold the release?",
        "task_id": "nimble-torrent-66",
        "task_title": "Deploy the migration",
        "choices": ("roll forward", "hold"),
    }
    values.update(overrides)
    return EscalationFacts(**values)


def digest_post(url: str = ORIGIN) -> str:
    window = DigestWindow(since=NOW - 3600, until=NOW)
    facts_ = (
        WorkFact(
            key="c1",
            kind=KIND_COMPLETED,
            category="work",
            project_id="p",
            task_id="t1",
            title="Task one",
            at=NOW - 60,
            detail="did the thing",
        ),
    )
    return build_digest(
        DigestInputs(window=window, facts=facts_), dashboard_url=url
    ).text


def every_post():
    """One rendered message per producer that posts to the human channel."""
    incident = facts()
    return {
        "escalation_root": render_root(
            incident, mentions=MentionPolicy(), base_url=ORIGIN, dedup_key="esc-1:root:0"
        ),
        "escalation_resolved": render_resolved_root(
            facts(state="resolved", revision=2, terminal_outcome="Rolled it forward"),
            dedup_key="esc-1:resolution:0:root",
        ),
        "escalation_opener": render_thread_opener(
            incident, base_url=ORIGIN, dedup_key="esc-1:thread:0"
        ),
        "escalation_resolution": render_resolution(
            facts(state="resolved", terminal_outcome="Rolled it forward"),
            base_url=ORIGIN,
            dedup_key="esc-1:resolution:1:thread",
        ),
        "digest": digest_post(),
        "review_ready": review_ready_post(
            revision=2,
            kind="spec",
            title="Discord as a chat extension of the supervisor",
            changes_note="Rev 2 answers the threading and quiet-hours questions.",
            base_url=ORIGIN,
            link_path=review_path("rev-1"),
        ),
        "status": status_line("⏸ offline — the daemon is not answering"),
    }


# ------------------------------------------------- no /settings/, one link each


@pytest.mark.parametrize("name", sorted(every_post()))
def test_no_renderer_ever_emits_a_settings_page(name):
    assert "/settings/" not in every_post()[name]


def test_the_escalation_post_links_the_incident_page_and_nothing_else():
    post = render_root(
        facts(), mentions=MentionPolicy(), base_url=ORIGIN, dedup_key="esc-1:root:0"
    )
    assert post.endswith(f"<{ORIGIN}/focus/escalations/esc-1>")
    assert "#escalation-reply-" not in post
    assert ORIGIN in post


def test_the_digest_links_the_needs_you_inbox():
    post = digest_post()
    assert post.endswith(f"<{ORIGIN}/focus/inbox>")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("escalation_root", f"{ORIGIN}/focus/escalations/esc-1"),
        ("escalation_opener", f"{ORIGIN}/focus/escalations/esc-1"),
        ("escalation_resolution", f"{ORIGIN}/focus/escalations/esc-1"),
        ("digest", f"{ORIGIN}/focus/inbox"),
        ("review_ready", f"{ORIGIN}/focus/reviews/rev-1"),
    ],
)
def test_every_linked_post_carries_exactly_one_link_and_it_is_last(name, expected):
    post = every_post()[name]
    assert post.count(ORIGIN) == 1, post
    assert post.splitlines()[-1] == f"<{expected}>"


def test_a_post_without_an_origin_says_why_instead_of_guessing_one():
    notice = "Remote dashboard link unavailable (no dashboard origin is configured)"
    post = render_root(
        facts(),
        mentions=MentionPolicy(),
        base_url="",
        dashboard_notice=notice,
        dedup_key="esc-1:root:0",
    )
    assert post.splitlines()[-1] == notice
    assert "http" not in post
    assert "127.0.0.1" not in post and "localhost" not in post


def test_the_rows_the_spec_gives_no_link_carry_none():
    """§3.2 gives the collapsed escalation and the thread replies no link column.

    The collapsed root's page is linked from the root above it and from the
    thread; a thread ack sits inside an incident that already linked.  Neither
    repeats it, which is what §3.1's "exactly one" asks for.
    """
    for post in (
        render_resolved_root(
            facts(state="resolved", terminal_outcome="Rolled it forward"),
            dedup_key="esc-1:resolution:0:root",
        ),
        render_ack(facts(), dedup_key="esc-1:ack:0"),
        render_relay("Rolled the replica forward", dedup_key="esc-1:relay:0"),
    ):
        assert "http" not in post
        assert "<" not in post


def test_thread_acknowledgements_carry_no_second_link():
    """§3.1 allows one link per post; the thread's root already named the page."""
    for post in (
        render_ack(facts(), dedup_key="esc-1:ack:0"),
        render_relay("Rolled the replica forward", dedup_key="esc-1:relay:0"),
    ):
        assert "http" not in post


def test_an_origin_that_cannot_be_carried_as_link_markup_degrades_to_the_notice():
    assert link_line("https://aq.example ts.net", "/focus/inbox") != ""
    assert "<" not in link_line("https://aq.example ts.net", "/focus/inbox")
    assert link_line("not a url at all", "/focus/inbox", notice="notice") == "notice"


# --------------------------------------------- the resolver is the only source


class _Config:
    def __init__(self, public_url: str) -> None:
        self.dashboard_server = type(
            "Server",
            (),
            {"enabled": True, "host": "127.0.0.1", "port": 8082, "public_url": public_url,
             "tailscale_path": ""},
        )()
        self.api_auth = type("Auth", (), {"trusted_dashboard_origins": ()})()


async def test_the_rendered_posts_name_exactly_the_origin_the_resolver_returned():
    resolver = DashboardLinkResolver(
        lambda: _Config("https://queue.tail1234.ts.net/"),
        probe=no_probe,
    )
    link = await resolver.resolve()
    assert link.url == "https://queue.tail1234.ts.net"

    for post in (
        render_root(
            facts(), mentions=MentionPolicy(), base_url=link.url, dedup_key="esc-1:root:0"
        ),
        digest_post(link.url),
    ):
        assert post.count(link.url) == 1
        assert "8082" not in post and "127.0.0.1" not in post


async def test_without_an_origin_every_producer_renders_the_resolver_notice():
    resolver = DashboardLinkResolver(lambda: _Config(""), probe=no_probe)
    link = await resolver.resolve()
    assert link.url == ""
    notice = link.unavailable_notice
    assert notice

    for post in (
        render_root(
            facts(),
            mentions=MentionPolicy(),
            base_url=link.url,
            dashboard_notice=notice,
            dedup_key="esc-1:root:0",
        ),
        build_digest(
            DigestInputs(
                window=DigestWindow(since=NOW - 3600, until=NOW),
                facts=(
                    WorkFact(
                        key="c1",
                        kind=KIND_COMPLETED,
                        category="work",
                        project_id="p",
                        task_id="t1",
                        title="Task one",
                        at=NOW - 60,
                        detail="did the thing",
                    ),
                ),
            ),
            dashboard_url=link.url,
            dashboard_notice=notice,
        ).text,
    ):
        assert post.splitlines()[-1] == notice
        assert "http" not in post


async def no_probe(_settings):
    raise AssertionError("a configured public URL must not need a tailnet probe")


def test_a_link_is_never_built_from_a_dashboard_link_that_never_resolved():
    """``DashboardLink(url="")`` is the "no origin" answer, not a relative link."""
    assert link_line(DashboardLink().url, escalation_path("esc-1"), notice="n") == "n"
    assert compose(["one"], STATUS_LINE, link=link_line("", "/focus/inbox")) == "one"