"""Retention sweep tests for smart test selection records, plus the operator
documentation completeness contract for the ``test_selection`` config section."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import TestSelectionConfig as _TestSelectionConfig

_CONFIGURATION_MD = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration.md"

def _test_selection_section() -> str:
    """The text of the ``test_selection`` section of the configuration reference."""
    lines = _CONFIGURATION_MD.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("## Smart test selection (`test_selection`)")
    except ValueError:
        raise AssertionError("## Smart test selection (`test_selection`) heading missing") from None
    for stop in range(start + 1, len(lines)):
        if lines[stop].startswith("## "):
            break
    else:
        stop = len(lines)
    return "\n".join(lines[start:stop])


@pytest.mark.parametrize("field", dataclasses.fields(_TestSelectionConfig))
def test_configuration_reference_names_every_test_selection_key(field):
    """A config key no operator can discover: the section table must name it."""
    assert f"`{field.name}`" in _test_selection_section(), (
        f"key {field.name!r} not named in the configuration reference"
    )


async def test_test_selection_retention_sweeps_once_per_hour(monkeypatch):
    from src.orchestrator.monitoring import MonitoringMixin

    now = 2_000_000.0
    monkeypatch.setattr("src.orchestrator.monitoring.time.time", lambda: now)

    class Harness(MonitoringMixin):
        pass

    harness = Harness()
    harness._last_test_selection_retention_sweep = 0.0
    harness.config = SimpleNamespace(
        test_selection=SimpleNamespace(enabled=True, retention_days=90)
    )
    harness.db = SimpleNamespace(
        delete_test_selections_older_than=AsyncMock(return_value=2)
    )

    await harness._sweep_test_selection_retention()
    await harness._sweep_test_selection_retention()

    harness.db.delete_test_selections_older_than.assert_awaited_once_with(
        older_than=now - 90 * 86_400.0
    )


async def test_test_selection_retention_respects_cadence(monkeypatch):
    from src.orchestrator.monitoring import MonitoringMixin

    now = 2_000_000.0
    monkeypatch.setattr("src.orchestrator.monitoring.time.time", lambda: now)

    class Harness(MonitoringMixin):
        pass

    harness = Harness()
    # A sweep ran 3599 s ago: inside the hour window.
    harness._last_test_selection_retention_sweep = now - 3599.0
    harness.config = SimpleNamespace(
        test_selection=SimpleNamespace(enabled=True, retention_days=90)
    )
    harness.db = SimpleNamespace(
        delete_test_selections_older_than=AsyncMock(return_value=0)
    )

    await harness._sweep_test_selection_retention()

    harness.db.delete_test_selections_older_than.assert_not_awaited()
    # The timestamp is not refreshed by a skipped sweep.
    assert harness._last_test_selection_retention_sweep == now - 3599.0


async def test_test_selection_retention_short_circuits_when_disabled():
    from src.orchestrator.monitoring import MonitoringMixin

    class Harness(MonitoringMixin):
        pass

    harness = Harness()
    harness._last_test_selection_retention_sweep = 0.0
    harness.config = SimpleNamespace(
        test_selection=SimpleNamespace(enabled=False, retention_days=90)
    )
    harness.db = SimpleNamespace(
        delete_test_selections_older_than=AsyncMock(return_value=0)
    )

    await harness._sweep_test_selection_retention()

    harness.db.delete_test_selections_older_than.assert_not_awaited()
    # Disabled: the timestamp is left untouched for the next enabled cycle.
    assert harness._last_test_selection_retention_sweep == 0.0


async def test_test_selection_retention_swallows_db_errors(monkeypatch, caplog):
    from src.orchestrator.monitoring import MonitoringMixin

    now = 2_000_000.0
    monkeypatch.setattr("src.orchestrator.monitoring.time.time", lambda: now)

    class Harness(MonitoringMixin):
        pass

    harness = Harness()
    harness._last_test_selection_retention_sweep = 0.0
    harness.config = SimpleNamespace(
        test_selection=SimpleNamespace(enabled=True, retention_days=90)
    )
    harness.db = SimpleNamespace(
        delete_test_selections_older_than=AsyncMock(side_effect=RuntimeError("boom"))
    )

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.monitoring"):
        await harness._sweep_test_selection_retention()

    # The failed sweep still consumed the hourly gate.
    await harness._sweep_test_selection_retention()
    harness.db.delete_test_selections_older_than.assert_awaited_once()
    assert any("Test selection retention sweep failed" in r.message for r in caplog.records)
