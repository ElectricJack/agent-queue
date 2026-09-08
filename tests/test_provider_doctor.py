# tests/test_provider_doctor.py
"""``aq doctor --check providers.claude_usage`` — implementation spec T7.

The Claude half of provider usage reads percentages out of English prose on
a ten-minute timer.  Two things rot silently: the timer stops firing, and the
CLI's wording moves under the regex.  Both leave the dashboard showing a
number that is old rather than wrong.  These tests pin the four verdicts that
tell those apart — and the one thing the check must *not* do, which is warn
about a probe an operator turned off on purpose.

Freshness is the age of the newest ``source='probe'`` snapshot, measured
against ``providers.claude.stale_after_seconds`` — the same number the
dashboard card draws, so doctor and the card never disagree about what
"stale" means.  The probe's recorded verdict (``record_probe_health``) is
read for the faults a snapshot cannot express: a body that did not parse,
and a probe that failed outright.
"""

from __future__ import annotations

import sys
import time

import pytest

import src.doctor  # noqa: F401 -- side effect: populates sys.modules
from src.doctor.models import Severity
from src.providers.snapshot import ProviderUsageSnapshot

# ``src/doctor/__init__.py`` imports the ``provider_checks`` *factory* out of
# this submodule, which rebinds the package attribute of the same name.  Any
# ``import src.doctor.provider_checks`` then resolves through that shadowed
# attribute and hands back the function.  ``sys.modules`` is untouched by the
# rebinding, so this is the unambiguous way to reach ``.CHECKS``/``.run_check``
# — same dance as ``tests/test_pool_doctor.py``.
provider_checks = sys.modules["src.doctor.provider_checks"]

CHECK_ID = "providers.claude_usage"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database
    from tests.db_fixtures import lease_dsn

    database = Database(lease_dsn(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def _config(*, enabled: bool = True, stale_after: int | None = None):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.providers.claude.usage_probe_enabled = enabled
    if stale_after is not None:
        cfg.providers.claude.stale_after_seconds = stale_after
    return cfg


async def _record_health(db, **overrides):
    """Write a probe verdict shaped exactly like ``_finish_probe`` writes one."""
    health = {
        "ok": True,
        "outcome": "probed",
        "unparsed": False,
        "not_applicable": False,
        "error": None,
        "recorded": 3,
        "ts": time.time(),
    }
    health.update(overrides)
    await db.record_probe_health("claude", health)
    return health


async def _seed_snapshots(db, *, observed_at: float | None = None) -> None:
    """The verified three-series reading, as the probe would have stored it."""
    when = time.time() if observed_at is None else observed_at
    await db.record_provider_usage(
        [
            ProviderUsageSnapshot(
                provider="claude",
                window="session",
                used_percent=5.0,
                observed_at=when,
                source="probe",
                account_label="max",
            ),
            ProviderUsageSnapshot(
                provider="claude",
                window="week",
                scope="all models",
                used_percent=45.0,
                observed_at=when,
                source="probe",
                account_label="max",
            ),
            ProviderUsageSnapshot(
                provider="claude",
                window="week",
                scope="Fable",
                used_percent=81.0,
                observed_at=when,
                source="probe",
                account_label="max",
            ),
        ]
    )


async def _run(db, cfg):
    return await provider_checks.run_check(db, CHECK_ID, config=cfg)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_catalog_holds_exactly_the_claude_usage_check():
    assert {c.id for c in provider_checks.CHECKS} == {CHECK_ID}
    assert all(c.owner == "provider-usage" for c in provider_checks.CHECKS)


def test_the_check_is_report_only():
    """T7's hard requirement: no ``--fix`` path.

    A ``fix`` that ran a probe would repair the *symptom* of a stalled timer
    and hide the timer, which is the whole thing the check exists to find.
    """
    check = next(c for c in provider_checks.CHECKS if c.id == CHECK_ID)
    assert check.fix is None
    assert not any(c.fix is not None for c in provider_checks.CHECKS)


def test_the_check_is_in_the_default_registry():
    from src.doctor import default_registry

    assert CHECK_ID in {c.id for c in default_registry().checks()}


# ---------------------------------------------------------------------------
# Healthy
# ---------------------------------------------------------------------------


async def test_a_fresh_parsed_probe_reports_the_percentages(db):
    await _seed_snapshots(db)
    await _record_health(db)

    finding = await _run(db, _config())

    assert finding.severity is Severity.OK
    assert "session 5%" in finding.detail
    assert "week (all models) 45%" in finding.detail
    assert "week (Fable) 81%" in finding.detail
    assert finding.data["series"] == 3
    assert finding.data["unparsed"] is False
    assert finding.fixable is False


async def test_unchanged_readings_refresh_probe_freshness(db):
    now = time.time()
    await _seed_snapshots(db, observed_at=now - 86_400)
    await _seed_snapshots(db, observed_at=now)
    await _record_health(db, recorded=0)

    finding = await _run(db, _config())

    assert finding.severity is Severity.OK
    assert finding.data["age_seconds"] < 60


async def test_a_stale_snapshot_warns_even_when_the_probe_verdict_is_fresh(db):
    """T7's freshness rule is about the snapshot, not the verdict.

    A probe that runs on time but leaves the newest reading a day old leaves
    the dashboard card just as frozen as one that never runs, so the age the
    horizon is compared against is the snapshot's.
    """
    await _seed_snapshots(db, observed_at=time.time() - 86_400)
    await _record_health(db, ts=time.time() - 60)

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert "1.0d ago" in finding.detail
    assert finding.data["age_seconds"] > 86_000
    assert finding.data["probe_verdict_age_seconds"] < 120


async def test_a_probe_that_has_stored_no_reading_warns(db):
    """A healthy-looking verdict with an empty table has nothing to draw."""
    await _record_health(db, recorded=0)

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert "stored no reading" in finding.detail
    assert finding.data["age_seconds"] is None


async def test_transcript_rows_do_not_count_as_probe_freshness(db):
    """Only ``source='probe'`` rows say the probe is still running."""
    await db.record_provider_usage(
        [
            ProviderUsageSnapshot(
                provider="claude",
                window="session",
                used_percent=5.0,
                observed_at=time.time(),
                source="transcript",
            )
        ]
    )
    await _record_health(db)

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert "stored no reading" in finding.detail


# ---------------------------------------------------------------------------
# Stale
# ---------------------------------------------------------------------------


async def test_a_snapshot_older_than_the_horizon_warns(db):
    now = time.time()
    await _seed_snapshots(db, observed_at=now - 4 * 3600)
    await _record_health(db, ts=now - 4 * 3600)

    finding = await _run(db, _config(stale_after=1500))

    assert finding.severity is Severity.WARN
    assert "4.0h ago" in finding.detail
    assert "not firing" in finding.detail
    assert finding.data["age_seconds"] > 1500
    assert finding.data["stale_after_seconds"] == 1500


async def test_the_horizon_comes_from_config(db):
    """One horizon, read from config, so doctor and the dashboard agree."""
    await _seed_snapshots(db, observed_at=time.time() - 1800)
    await _record_health(db)

    assert (await _run(db, _config(stale_after=1500))).severity is Severity.WARN
    assert (await _run(db, _config(stale_after=7200))).severity is Severity.OK


async def test_no_probe_has_ever_run_warns(db):
    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert finding.data["probe_ran"] is False
    assert "provider-usage-probe" in finding.detail


# ---------------------------------------------------------------------------
# The wording moved
# ---------------------------------------------------------------------------


async def test_an_unparsed_last_probe_warns(db):
    await _seed_snapshots(db)
    await _record_health(db, outcome="unparsed", unparsed=True, recorded=0)

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert finding.data["unparsed"] is True
    assert "src/providers/claude_usage.py" in finding.detail


async def test_unparsed_beats_stale_in_the_message(db):
    """A probe unparsed for a day is both; the regex is the actionable half."""
    await _record_health(db, outcome="unparsed", unparsed=True, ts=time.time() - 86_400)

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert "wording moved" in finding.detail
    assert "1.0d ago" in finding.detail


async def test_a_failed_last_probe_warns_with_its_error(db):
    await _record_health(
        db,
        ok=False,
        outcome="timeout",
        error="claude /usage did not answer within 20s",
    )

    finding = await _run(db, _config())

    assert finding.severity is Severity.WARN
    assert "did not answer within 20s" in finding.detail
    assert finding.data["outcome"] == "timeout"


# ---------------------------------------------------------------------------
# Not a fault
# ---------------------------------------------------------------------------


async def test_a_disabled_probe_is_ok_not_warn(db):
    """T7 acceptance: disabling the probe is a decision, not a defect.

    Nothing has probed, so every staleness rule would fire — the config gate
    has to be read before any of them.
    """
    finding = await _run(db, _config(enabled=False))

    assert finding.severity is Severity.OK
    assert finding.data["enabled"] is False
    assert "usage_probe_enabled is false" in finding.detail


async def test_a_disabled_probe_stays_ok_with_a_long_stale_verdict_on_record(db):
    await _record_health(db, ts=time.time() - 86_400, unparsed=True)

    assert (await _run(db, _config(enabled=False))).severity is Severity.OK


async def test_a_box_without_the_cli_is_info(db):
    await _record_health(db, outcome="unavailable", recorded=0, detail="claude: not found")

    finding = await _run(db, _config())

    assert finding.severity is Severity.INFO
    assert finding.data["outcome"] == "unavailable"


async def test_an_api_key_account_is_info(db):
    await _record_health(db, outcome="not_applicable", not_applicable=True, recorded=0)

    finding = await _run(db, _config())

    assert finding.severity is Severity.INFO
    assert "no subscription window" in finding.detail


async def test_no_database_is_info_not_a_traceback():
    finding = await provider_checks.run_check(None, CHECK_ID, config=_config())

    assert finding.severity is Severity.INFO
    assert "database not initialised" in finding.detail
