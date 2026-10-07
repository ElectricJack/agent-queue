"""A full `aq doctor` must not hit the CLI read timeout before its checks do."""

from src.cli.client import _COMMAND_TIMEOUTS, _DEFAULT_TIMEOUT
from src.doctor import default_registry
from src.doctor.db_checks import db_checks


def test_doctor_read_timeout_outlasts_every_check_timeout():
    checks = list(default_registry().checks()) + list(db_checks())
    longest = max(check.timeout_s for check in checks)
    timeout = _COMMAND_TIMEOUTS.get("doctor", _DEFAULT_TIMEOUT)
    assert timeout > longest, (timeout, longest)


def test_train_controls_outlast_the_default_read_timeout():
    """Train controls observe every pending root before answering (2026-10-06)."""
    for name in ("integration_seal_now", "integration_refresh_epic",
                 "integration_retire_origin", "integration_pause_batch",
                 "integration_resume_batch", "integration_abort_batch",
                 "integration_eject"):
        assert _COMMAND_TIMEOUTS.get(name, _DEFAULT_TIMEOUT) > _DEFAULT_TIMEOUT, name
