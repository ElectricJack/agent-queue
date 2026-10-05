"""The §4 supervisor-authored digest window (2026-10-03 §4.1-4.3, §7.1 P3).

Every test drives the real ``digest_windows`` outbox and the real
``supervisor_report_requests`` rows against :class:`SinkTransport`, with an
injected clock and no gateway.  The properties are the ones the spec states as
absolutes and the ones an append-only channel punishes a false claim for:

* **window dedup** -- one window is one row, one author request and one post,
  however many evaluators, ticks or restarts touch it;
* **quiet hours** -- no post overnight, and *the facts are still collected*,
  because the 07:00 report has to describe the night;
* **suppression** -- an unchanged fleet with nothing waiting on Jack is silent,
  and after three such windows one line is allowed per day, so silence stays
  distinguishable from a dead bot;
* **fallback** -- ten minutes after a window closes, the deterministic digest
  for the same facts posts whether or not the supervisor wrote anything.

The pure policy (:mod:`src.digest.supervisor`) is exercised directly where a
rule is a table; the durable half is exercised through the real service, and the
author surface through the real ``CommandHandler``.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import (
    CapabilityPolicy,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import (
    AppConfig,
    DatabaseConfig,
    DiscordConfig,
    DiscordDigestConfig,
    DiscordEscalationConfig,
    ReportQuietHoursConfig,
    ReportsConfig,
)
from src.database import Database
from src.database.tables import doc_reviews, escalations, messages, tasks
from src.digest import DigestScheduleService, schedule_for
from src.digest.supervisor import (
    QUIET_LINE,
    SEND_QUIET_LINE,
    SKIP_QUIET_HOURS,
    SKIP_UNCHANGED,
    author_deadline,
    bounded_facts,
    decide_window,
    facts_hash,
    quiet_at,
    quiet_line,
    render_authored_digest,
)
from src.escalations.transport import SinkTransport
from src.event_bus import EventBus
from src.models import Project, Task, TaskCompletion
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

CHANNEL = "424242424242424242"
CADENCE = 120.0 * 60.0
FALLBACK = 10 * 60.0
BASE_URL = "https://queue.example.test"
ZONE = "America/Los_Angeles"
#: The §4.3 line's own wording, asserted rather than restated.
QUIET_DAY = "2026-10-03"
#: A fixed instant for the rules that are pure arithmetic.  Nothing durable is
#: built on it, so it can sit inside the quiet interval on purpose.
FIXED = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo(ZONE)).timestamp()
NIGHT = datetime(2026, 10, 3, 2, 30, tzinfo=ZoneInfo(ZONE)).timestamp()
MIDDAY = FIXED
BASE = FIXED


def live_anchor() -> float:
    """A cadence boundary at or before now, so a held window is still open.

    The command surface takes its own clock from the wall, so a window whose
    deadline has already passed cannot be posted at.  Anchoring the injected
    clock here keeps every deadline comfortably in the future, which is what a
    real evaluation of a just-opened window looks like.
    """
    return math.floor(time.time() / CADENCE) * CADENCE


#: §4.1's shipped quiet interval.
QUIET = ReportQuietHoursConfig(start="22:00", end="07:00")


def make_digest(**overrides) -> DiscordDigestConfig:
    """The §4 defaults, with no quiet hours unless a test asks for them.

    Quiet hours are a wall-clock rule, so a suite that inherited them would
    assert against whatever time of day it happened to run at.
    """
    values = {
        "enabled": True,
        "interval_minutes": 60,
        "supervisor_authored": True,
        "cadence_minutes": 120,
        "quiet_hours": None,
        "author_fallback_minutes": 10,
        "quiet_line_after_skips": 3,
    }
    values.update(overrides)
    return DiscordDigestConfig(**values)


def make_config(digest=None, *, channel_id: str = CHANNEL) -> DiscordConfig:
    return DiscordConfig(
        channel_id=channel_id,
        digest=digest or make_digest(),
        escalation=DiscordEscalationConfig(enabled=True),
    )


class Clock:
    def __init__(self, now: float | None = None) -> None:
        # Just past the boundary that follows the anchor, so the first window is
        # due and its deadline is still ahead of the wall clock.
        self.now = live_anchor() + CADENCE + 30 if now is None else now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
async def db():
    database = Database(lease_dsn("digest-supervisor"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Agent Queue"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


@pytest.fixture
async def env(tmp_path, db):
    """A real ``CommandHandler`` over the same database the service uses."""
    config = AppConfig(
        discord=make_config(),
        reports=ReportsConfig(timezone=ZONE),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url="postgresql+asyncpg://localhost/unused"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.bus = EventBus(env="dev")
    yield CommandHandler(orch, config), db, config


def make_service(db, transport, config=None, *, clock=None, owner="daemon-a", anchor=None):
    """The real dispatcher over the real config this install would use."""
    config = config or AppConfig(
        discord=make_config(), reports=ReportsConfig(timezone=ZONE), data_dir="/tmp"
    )
    service = DigestScheduleService(
        db,
        transport,
        config=config,
        lease_owner=owner,
        base_url=BASE_URL,
        clock=clock or Clock(),
    )
    # A fresh generation anchors where it was first observed and never reaches
    # back over windows a previous one owned (§9).
    key = (f"discord:{CHANNEL}", schedule_for(config.discord).generation)
    service._anchors.setdefault(key, live_anchor() if anchor is None else anchor)
    return service


async def complete(db, task_id, *, at=None, summary="shipped the thing", project_id="p"):
    at = live_anchor() + 600 if at is None else at
    await db.create_task(Task(id=task_id, project_id=project_id, title=task_id, description=""))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status="COMPLETED"))
    await db.save_task_completion(
        TaskCompletion(
            id="completion-" + uuid4().hex,
            task_id=task_id,
            outcome="pass",
            summary=summary,
            completed_at=at,
        )
    )


async def open_review(db, review_id="rev-1", *, project_id="p", created=None, decider="user"):
    created = live_anchor() + 30 if created is None else created
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(doc_reviews).values(
                id=review_id,
                project_id=project_id,
                title="Discord as chat",
                kind="spec",
                decider=decider,
                state="in_review",
                current_revision=1,
                vault_path=f"reviews/{review_id}.md",
                created_at=created,
                updated_at=created,
            )
        )


async def windows(db, **kwargs):
    return await db.list_digest_windows(destination=f"discord:{CHANNEL}", **kwargs)


async def only_window(db):
    rows = await windows(db)
    assert len(rows) == 1, rows
    return rows[0]


def request_id_for(window_id: str) -> str:
    return f"report-digest-{window_id}"


async def digest_request(db, window):
    found = await db.find_digest_request(
        destination=f"discord:{CHANNEL}",
        generation=window["config_generation"],
        window_start=float(window["window_start"]),
    )
    assert found is not None, "the window has no author request"
    return found[1]


async def wake_message(db, request_id):
    async with db._engine.connect() as conn:
        row = (
            (await conn.execute(select(messages).where(messages.c.id == f"msg-{request_id}")))
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def service_principal() -> ExecutionPrincipal:
    """The install-wide principal the ``supervisor-digest`` playbook runs as."""
    return ExecutionPrincipal(
        kind=PrincipalKind.SERVICE,
        policy=CapabilityPolicy(aq_commands=frozenset({"digest_request"})),
        service_name="supervisor-digest",
        project_id=None,
    )


def supervisor_principal() -> ExecutionPrincipal:
    """The live global supervisor: the §4 author."""
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy(aq_commands=frozenset({"digest_facts", "digest_post"})),
        session_id="s-global",
        project_id=None,
        elevated=True,
    )


def posted_text(transport: SinkTransport) -> str:
    assert len(transport.messages) == 1, sorted(transport.messages)
    return next(iter(transport.messages.values())).content


# ------------------------------------------------------------ pure policy


def test_quiet_hours_wrap_midnight_in_the_reports_timezone():
    def local(hour: int, minute: int = 0) -> float:
        return datetime(2026, 10, 3, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()

    assert quiet_at(local(23), ZONE, QUIET)
    assert quiet_at(NIGHT, ZONE, QUIET)
    assert quiet_at(local(6), ZONE, QUIET)
    assert not quiet_at(local(7), ZONE, QUIET)
    assert not quiet_at(MIDDAY, ZONE, QUIET)
    # No quiet hours configured is never quiet.
    assert not quiet_at(local(23), ZONE, None)


def test_the_authored_body_is_cut_to_the_digest_budget_and_carries_one_link():
    body = "\n".join(f"line {index}" for index in range(9))
    text = render_authored_digest(body, base_url=BASE_URL)
    assert len(text) <= 600
    assert text.count("https://") == 1
    assert text.endswith(f"<{BASE_URL}/focus/inbox>")
    # §3.1's glyph is added, and a mention an author typed cannot ping a role:
    # the bare ``@`` survives with a zero-width space, so no token Discord would
    # resolve as a role or user ping ever reaches the channel.
    assert text.startswith("📊 ")
    mentioned = render_authored_digest("@everyone shipped", base_url=BASE_URL)
    assert "<@&" not in mentioned and "@everyone" not in mentioned
    assert "everyone" in mentioned


def test_an_empty_body_is_refused_rather_than_posted():
    with pytest.raises(ValueError):
        render_authored_digest("   \n  ", base_url=BASE_URL)


def _idle_brief(**overrides):
    window = type("Window", (), {"since": BASE, "until": BASE + CADENCE})()
    values = {
        "landed": [],
        "stuck": [],
        "escalations": [],
        "reviews": [],
        "sessions_working": 4,
        "sessions_total": 9,
        "active_tasks": 4,
        "open_escalations": 0,
    }
    values.update(overrides)
    return bounded_facts(window=window, **values)


def _decide(brief, **overrides):
    common = {
        "facts": brief,
        "has_activity": False,
        "previous_facts_hash": facts_hash(brief),
        "previous_quiet_line_day": None,
        "consecutive_skips": 0,
        "quiet_now": False,
        "now": MIDDAY,
        "timezone": ZONE,
        "quiet_line_after": 3,
    }
    common.update(overrides)
    return decide_window(**common)


def test_an_unchanged_fleet_is_silent_and_three_windows_earn_one_line_a_day():
    brief = _idle_brief()
    first = _decide(brief, previous_facts_hash=None, consecutive_skips=0)
    assert (first.action, first.consecutive_skips) == (SKIP_UNCHANGED, 1)

    second = _decide(brief, consecutive_skips=1)
    assert (second.action, second.consecutive_skips) == (SKIP_UNCHANGED, 2)

    third = _decide(brief, consecutive_skips=2)
    assert third.action == SEND_QUIET_LINE
    assert third.quiet_line_due
    # The line spends the streak rather than adding to it.
    assert third.consecutive_skips == 0

    # Once the day's line is spent, the streak starts again silently.
    spent = _decide(brief, previous_quiet_line_day=QUIET_DAY, consecutive_skips=0)
    assert (spent.action, spent.consecutive_skips) == (SKIP_UNCHANGED, 1)


def test_quiet_hours_are_not_a_skip_and_spend_no_streak():
    decision = _decide(_idle_brief(), quiet_now=True, consecutive_skips=2)
    assert decision.action == SKIP_QUIET_HOURS
    assert decision.quiet_line_due is False
    assert decision.consecutive_skips == 0


@pytest.mark.parametrize("unchanged", [False, True])
def test_something_waiting_on_jack_outranks_the_unchanged_fleet(unchanged):
    idle = _idle_brief()
    waiting = _idle_brief(reviews=[{"id": "rev-1"}])
    decision = _decide(
        waiting,
        previous_facts_hash=facts_hash(waiting if unchanged else idle),
        consecutive_skips=2,
    )
    assert decision.action == "hold"
    assert decision.consecutive_skips == 0
    assert decision.quiet_line_due is False


def test_the_quiet_line_is_the_specs_one_line_with_the_session_count():
    line = quiet_line(4, base_url=BASE_URL)
    assert QUIET_LINE.format(sessions=4) in line
    assert line.startswith("⏳ ")
    assert line.endswith(f"<{BASE_URL}/focus/inbox>")
    assert len(line) <= 600


# ------------------------------------------------------------ window dedup


async def test_one_window_is_one_row_and_one_author_request_however_often_it_is_ticked(db):
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)

    for _ in range(3):
        await service.tick()

    window = await only_window(db)
    assert window["send_status"] == "pending", "a held window is not delivered at evaluation"
    assert float(window["due_at"]) == author_deadline(window["window_end"], FALLBACK)
    request = await digest_request(db, window)
    assert request["kind"] == "digest"
    assert request["state"] == "reserved"
    assert request["id"] == request_id_for(window["id"])
    # Nothing was posted: the window is held for its author.
    assert transport.messages == {}


async def test_a_second_daemon_on_the_same_boundary_reserves_the_same_window(db):
    await complete(db, "t1")
    clock = Clock()
    first = make_service(db, SinkTransport(), clock=clock, owner="daemon-a")
    second = make_service(db, SinkTransport(), clock=Clock(clock() + 900), owner="daemon-b")

    assert (await first.tick()).evaluated == 1
    assert (await second.tick()).evaluated == 0
    assert len(await windows(db)) == 1


async def test_windows_sit_on_the_two_hour_grid_and_one_post_reaches_the_channel(db):
    await complete(db, "t1")
    anchor = live_anchor()
    clock = Clock(anchor + CADENCE + 30)
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock, anchor=anchor)
    await service.tick()

    window = await only_window(db)
    assert (float(window["window_start"]), float(window["window_end"])) == (
        anchor,
        anchor + CADENCE,
    )

    # Ten minutes later the deterministic fallback posts the same facts.
    clock.advance(FALLBACK)
    assert (await service.tick()).sent == 1
    assert "shipped the thing" in posted_text(transport)
    assert (await only_window(db))["send_status"] == "sent"


# ------------------------------------------------------------ quiet hours


async def test_needs_you_excludes_internal_object_reviews(db):
    await open_review(db, "rev-internal", decider="supervisor")
    await open_review(db, "rev-result", decider="user")
    await open_review(db, "rev-delegated", decider="user_or_supervisor")
    transport = SinkTransport()
    await make_service(db, transport).tick()
    request = await digest_request(db, await only_window(db))
    reviews = request["brief"]["needs_you"]["reviews"]["items"]
    assert {row["id"] for row in reviews} == {"rev-result", "rev-delegated"}
    assert request["brief"]["counts"]["needs_you"] == 2


async def test_needs_you_excludes_delivery_incidents_and_answered_escalations(db):
    for state in ("needs_human", "stale", "reply_received", "resolving", "resolved", "cancelled"):
        row, _ = await db.create_escalation(
            id=f"esc-{state}",
            project_id="p",
            source_kind="question",
            source_identity=state,
            incident_key=f"question:{state}",
            supervisor_owner="supervisor-p",
            summary="Choose a branch",
            investigation="Two branches contain the fix",
            decision_requested="Which branch should ship?",
            severity="medium",
            now=live_anchor() + 30,
        )
        values = {"state": state}
        if state in ("stale", "resolved", "cancelled"):
            values.update(terminal_at=live_anchor() + 60, terminal_outcome=state)
        async with db._engine.begin() as conn:
            await conn.execute(
                update(escalations).where(escalations.c.id == row["id"]).values(values)
            )
    await db.create_escalation(
        id="esc-delivery",
        project_id="p",
        source_kind="supervisor_delivery",
        source_identity="undelivered",
        incident_key="supervisor-unavailable",
        supervisor_owner="supervisor-p",
        summary="Restore supervisor delivery",
        investigation="The supervisor is offline",
        decision_requested="Restore delivery",
        severity="low",
        now=live_anchor() + 30,
    )
    transport = SinkTransport()
    await make_service(db, transport).tick()
    request = await digest_request(db, await only_window(db))
    needed = request["brief"]["needs_you"]["escalations"]["items"]
    assert {item["id"] for item in needed} == {"esc-needs_human", "esc-stale"}
    assert request["brief"]["counts"]["needs_you"] == 2
    assert not transport.messages


async def test_a_quiet_hour_posts_nothing_but_still_freezes_the_facts(db):
    await complete(db, "t1", at=NIGHT + 600)
    clock = Clock(NIGHT + CADENCE + 30)
    transport = SinkTransport()
    service = make_service(
        db,
        transport,
        clock=clock,
        anchor=NIGHT,
        config=AppConfig(
            discord=make_config(make_digest(quiet_hours=QUIET)),
            reports=ReportsConfig(timezone=ZONE),
            data_dir="/tmp",
        ),
    )

    report = await service.tick()
    assert report.suppressed == 1
    assert report.sent == 0
    assert transport.messages == {}

    window = await only_window(db)
    assert window["send_status"] == "suppressed"
    assert window["suppression_reason"] == SKIP_QUIET_HOURS
    cursor = window["activity_cursor"]
    # Fact collection is not what quiet hours suppress: the night is described by
    # the 07:00 report, which reads what this window recorded.
    assert cursor["facts_hash"]
    assert cursor["fact_count"] >= 1


async def test_the_same_fleet_outside_quiet_hours_is_held_for_its_author(db):
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)

    assert (await service.tick()).evaluated == 1
    window = await only_window(db)
    assert window["suppression_reason"] is None
    assert (await digest_request(db, window))["kind"] == "digest"


# -------------------------------------------------- suppression + quiet line


async def test_three_quiet_windows_post_the_line_once_and_then_go_quiet_again(db):
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)

    # No activity at all: three unchanged windows, the third spending the streak.
    for _ in range(3):
        await service.tick()
        clock.advance(CADENCE)

    rows = list(reversed(await windows(db)))
    assert [row["suppression_reason"] for row in rows[:3]] == [
        SKIP_UNCHANGED,
        SKIP_UNCHANGED,
        None,
    ]
    assert QUIET_LINE.format(sessions=0) in posted_text(transport)

    # A fourth unchanged window stays silent: one line per day, not one per skip.
    await service.tick()
    clock.advance(CADENCE)
    await service.tick()
    assert len(transport.messages) == 1
    latest = (await windows(db))[0]
    assert latest["send_status"] == "suppressed"
    assert latest["suppression_reason"] == SKIP_UNCHANGED


# ------------------------------------------------------------- the fallback


async def test_the_fallback_is_owed_exactly_ten_minutes_after_the_window_closes(db):
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, clock=clock)
    await service.tick()

    window = await only_window(db)
    assert float(window["due_at"]) == float(window["window_end"]) + FALLBACK

    # Nine minutes in, nothing is owed yet.
    clock.advance(FALLBACK - 60)
    assert (await service.tick()).sent == 0
    assert transport.messages == {}

    clock.advance(60)
    assert (await service.tick()).sent == 1
    assert len(transport.messages) == 1


async def test_a_wake_the_author_never_answers_leaves_the_fallback_to_post(db, env):
    handler, _db, config = env
    await complete(db, "t1", summary="the knowledge-records restore")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, config=config, clock=clock)
    await service.tick()
    window = await only_window(db)

    # The playbook hands the window to the supervisor's inbox; nothing posts.
    with principal_context(service_principal()):
        assert (await handler.execute("digest_request", {}))["requested"] == 1
    message = await wake_message(db, request_id_for(window["id"]))
    assert message is not None
    assert message["to_kind"] == "session"
    assert "aq digest facts --since" in message["body"]
    assert "aq digest post --window" in message["body"]

    clock.advance(FALLBACK)
    assert (await service.tick()).sent == 1
    assert "the knowledge-records restore" in posted_text(transport)
    assert (await digest_request(db, window))["state"] == "fallback"


async def test_the_supervisor_posts_before_the_deadline_and_the_fallback_stays_silent(db, env):
    handler, _db, config = env
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, config=config, clock=clock)
    await service.tick()
    window = await only_window(db)
    start = float(window["window_start"])

    with principal_context(supervisor_principal()):
        facts = await handler.execute("digest_facts", {"since": start})
        assert facts["success"] is True
        assert facts["window_id"] == window["id"]
        assert facts["facts"]["counts"]["landed"] == 1
        assert facts["facts"]["landed"]["items"][0]["task_id"] == "t1"
        assert facts["seconds_remaining"] > 0

        posted = await handler.execute(
            "digest_post",
            {
                "window": start,
                "body": "Landed the digest change.\nNothing is stuck.\nNeeds you: nothing.",
            },
        )
    assert posted["success"] is True
    assert posted["state"] == "submitted"
    assert posted["characters"] <= 600

    # The post is delivered on the next pass, and only once.
    assert (await service.tick()).sent == 1
    text = posted_text(transport)
    assert "Landed the digest change." in text
    assert "shipped the thing" not in text, "the fallback replaced the author"

    clock.advance(FALLBACK * 2)
    assert (await service.tick()).sent == 0
    assert len(transport.messages) == 1


async def test_a_body_with_a_link_or_nothing_to_say_is_refused(db, env):
    handler, _db, config = env
    await complete(db, "t1")
    clock = Clock()
    service = make_service(db, SinkTransport(), config=config, clock=clock)
    await service.tick()
    start = float((await only_window(db))["window_start"])
    with principal_context(service_principal()):
        await handler.execute("digest_request", {})
    with principal_context(supervisor_principal()):
        linked = await handler.execute(
            "digest_post", {"window": start, "body": "Landed it <https://elsewhere.test>"}
        )
        empty = await handler.execute("digest_post", {"window": start, "body": "   "})
    assert linked["error_code"] == "digest.invalid"
    assert empty["error_code"] == "digest.invalid"
    assert (await only_window(db))["send_status"] == "pending"


async def test_a_closed_window_refuses_a_second_post(db, env):
    handler, _db, config = env
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, config=config, clock=clock)
    await service.tick()
    start = float((await only_window(db))["window_start"])
    with principal_context(service_principal()):
        await handler.execute("digest_request", {})
    with principal_context(supervisor_principal()):
        first = await handler.execute(
            "digest_post", {"window": start, "body": "Landed it. Nothing stuck."}
        )
        second = await handler.execute(
            "digest_post", {"window": start, "body": "A different summary."}
        )
    assert first["success"] is True
    assert second["error_code"] == "digest.closed"
    await service.tick()
    assert len(transport.messages) == 1


async def test_the_author_surface_is_refused_with_the_flag_off(db, env):
    handler, _db, config = env
    config.discord.digest.supervisor_authored = False
    with principal_context(supervisor_principal()):
        facts = await handler.execute("digest_facts", {"since": BASE})
        post = await handler.execute("digest_post", {"window": BASE, "body": "hello"})
    assert facts["error_code"] == "digest.disabled"
    assert post["error_code"] == "digest.disabled"


async def test_a_project_scoped_session_cannot_read_the_fleet_window(db, env):
    handler, _db, _config = env
    scoped = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy(aq_commands=frozenset({"digest_facts"})),
        session_id="s-p",
        project_id="p",
    )
    with principal_context(scoped):
        result = await handler.execute("digest_facts", {"since": BASE})
    assert result["error_code"] == "out_of_scope"


async def test_turning_the_flag_off_releases_a_held_window_to_the_fallback(db, env):
    handler, _db, config = env
    await complete(db, "t1")
    clock = Clock()
    transport = SinkTransport()
    service = make_service(db, transport, config=config, clock=clock)
    await service.tick()
    assert (await only_window(db))["send_status"] == "pending"

    config.discord.digest.supervisor_authored = False
    assert (await service.tick()).sent == 1
    assert "shipped the thing" in posted_text(transport)
    with principal_context(service_principal()):
        released = await handler.execute("digest_request", {})
    assert released["success"] is True
