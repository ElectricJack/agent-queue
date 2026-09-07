"""What the ``/usage`` text is allowed to do to us.

The wording of ``claude -p "/usage"`` is a CLI implementation detail that
will change under us, so most of these pin the *failure* behaviour: an
unknown scope must survive without a code change, and unreadable text must
produce nothing at all rather than a guess the dashboard would render as
fact.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.providers import ProviderUsageSnapshot, parse_usage_text

LA = ZoneInfo("America/Los_Angeles")

# The verified output of claude 2.1.263 on this box.
SAMPLE = """You are currently using your subscription to power your Claude Code usage

Current session: 5% used · resets Sep 7, 3:59pm (America/Los_Angeles)
Current week (all models): 45% used · resets Sep 9, 2:59pm (America/Los_Angeles)
Current week (Fable): 81% used · resets Sep 9, 2:59pm (America/Los_Angeles)
"""

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=LA).timestamp()


def test_the_verified_sample_yields_one_snapshot_per_limit_line():
    parse = parse_usage_text(SAMPLE, now=NOW)

    assert parse.unparsed is False
    assert [(s.window, s.scope, s.used_percent) for s in parse.snapshots] == [
        ("session", "", 5.0),
        ("week", "all models", 45.0),
        ("week", "Fable", 81.0),
    ]
    assert {s.provider for s in parse.snapshots} == {"claude"}
    assert {s.source for s in parse.snapshots} == {"probe"}
    assert {s.observed_at for s in parse.snapshots} == {NOW}


def test_the_sample_resets_resolve_to_the_printed_local_times():
    session, week, _ = parse_usage_text(SAMPLE, now=NOW).snapshots

    assert session.resets_at == datetime(2026, 9, 7, 15, 59, tzinfo=LA).timestamp()
    assert week.resets_at == datetime(2026, 9, 9, 14, 59, tzinfo=LA).timestamp()
    assert session.resets_at < week.resets_at


def test_an_unseen_scope_needs_no_code_change():
    parse = parse_usage_text("Current week (Opus): 3% used", now=NOW)

    assert parse.unparsed is False
    assert parse.snapshots == [
        ProviderUsageSnapshot(
            provider="claude",
            window="week",
            scope="Opus",
            used_percent=3.0,
            resets_at=None,
            observed_at=NOW,
            source="probe",
        )
    ]


def test_garbage_text_records_nothing_and_flags_itself():
    parse = parse_usage_text("Error: unknown slash command\n/usage\n", now=NOW)

    assert parse.snapshots == []
    assert parse.unparsed is True


def test_empty_text_is_a_parse_failure_too():
    assert parse_usage_text("   \n", now=NOW).unparsed is True


def test_a_reworded_preamble_alone_is_a_parse_failure():
    # The preamble surviving while the limit lines change shape is exactly
    # the drift the caller must notice.
    parse = parse_usage_text(
        "You are currently using your subscription to power your Claude Code usage\n",
        now=NOW,
    )

    assert parse.snapshots == []
    assert parse.unparsed is True


def test_an_api_key_account_is_not_applicable_rather_than_broken():
    parse = parse_usage_text(
        "You are currently using an API key to power your Claude Code usage\n"
        "Manage your credit balance at console.anthropic.com\n",
        now=NOW,
    )

    assert parse.snapshots == []
    assert parse.unparsed is False


def test_an_unreadable_reset_costs_the_clock_not_the_percentage():
    parse = parse_usage_text("Current session: 5% used · resets whenever it feels like it", now=NOW)

    assert parse.unparsed is False
    assert parse.snapshots[0].used_percent == 5.0
    assert parse.snapshots[0].resets_at is None


def test_an_unknown_timezone_falls_back_to_the_caller_default():
    line = "Current session: 5% used · resets Sep 7, 3:59pm (Mars/Olympus_Mons)"

    parse = parse_usage_text(line, now=NOW, tz_default="America/Los_Angeles")

    assert parse.snapshots[0].resets_at == datetime(2026, 9, 7, 15, 59, tzinfo=LA).timestamp()


def test_a_december_reset_seen_in_january_resolves_forward():
    jan_first = datetime(2027, 1, 1, 9, 0, tzinfo=LA).timestamp()

    parse = parse_usage_text(
        "Current week (all models): 45% used · resets Dec 31, 11:00pm (America/Los_Angeles)",
        now=jan_first,
    )

    resets_at = parse.snapshots[0].resets_at
    assert resets_at is not None
    assert resets_at > jan_first
    assert datetime.fromtimestamp(resets_at, LA) == datetime(2027, 12, 31, 23, 0, tzinfo=LA)


def test_a_january_reset_seen_in_december_rolls_into_the_next_year():
    new_years_eve = datetime(2026, 12, 31, 22, 0, tzinfo=LA).timestamp()

    parse = parse_usage_text(
        "Current session: 5% used · resets Jan 1, 2:00am (America/Los_Angeles)",
        now=new_years_eve,
    )

    resets_at = parse.snapshots[0].resets_at
    assert datetime.fromtimestamp(resets_at, LA) == datetime(2027, 1, 1, 2, 0, tzinfo=LA)


def test_a_fractional_percentage_survives():
    parse = parse_usage_text("Current week (all models): 45.5% used", now=NOW)

    assert parse.snapshots[0].used_percent == 45.5


def test_a_midnight_reset_reads_as_zero_hundred_not_noon():
    parse = parse_usage_text(
        "Current session: 5% used · resets Sep 8, 12:30am (America/Los_Angeles)", now=NOW
    )

    assert datetime.fromtimestamp(parse.snapshots[0].resets_at, LA) == datetime(
        2026, 9, 8, 0, 30, tzinfo=LA
    )


def test_a_noon_reset_reads_as_twelve_hundred():
    parse = parse_usage_text(
        "Current session: 5% used · resets Sep 7, 12:30pm (America/Los_Angeles)", now=NOW
    )

    assert datetime.fromtimestamp(parse.snapshots[0].resets_at, LA) == datetime(
        2026, 9, 7, 12, 30, tzinfo=LA
    )


def test_surrounding_chatter_does_not_disturb_the_limit_lines():
    noisy = (
        "Some new banner we have never seen\n"
        "\n"
        "  Current session: 5% used · resets Sep 7, 3:59pm (America/Los_Angeles)  \n"
        "Learn more at https://example.invalid/usage\n"
    )

    parse = parse_usage_text(noisy, now=NOW)

    assert len(parse.snapshots) == 1
    assert parse.snapshots[0].used_percent == 5.0
