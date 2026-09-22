"""The integration troubleshooting guide teaches stranded branch-owner recovery.

See spec ``2026-09-21-integration-owner-recovery-design.md`` and Task 5 of
``2026-09-21-integration-owner-recovery.md``: the guide must describe what a
stranded owner is, how ``aq integration release-owner`` proves and releases
one (with ``--dry-run``), the automatic sweep switch, every named refusal
reason, where preserved work lands, and how to read
``integration_owner_recoveries`` — replacing the old story that retained
owners only leave through ``LOCAL operator`` "guarded integration ownership
controls".
"""

import re
from pathlib import Path

DOCS = Path(__file__).parents[1] / "docs" / "guides"


def _guide() -> str:
    return (DOCS / "integration-troubleshooting.md").read_text(encoding="utf-8")


def _owner_section(text: str) -> str:
    """The section for a stranded/retained branch owner, up to the next H2."""
    # Anchor on the H2 whose own line names the branch owner: `[^\n]*` keeps the
    # match from backtracking to an earlier H2 and sweeping in other sections.
    match = re.search(r"^## [^\n]*owns its branch.*?(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert match, "the guide no longer has a branch-owner section"
    return match.group(0)


def test_guide_mentions_release_owner_command_and_dry_run():
    section = _owner_section(_guide())
    assert "aq integration release-owner" in section
    assert "--dry-run" in section


def test_guide_mentions_the_sweep_switch_and_that_it_ships_off():
    section = _owner_section(_guide())
    assert "integration.owner_recovery_sweep" in section
    assert "off by default" in section or "ships off" in section


def test_guide_names_every_refusal_reason_and_its_fix():
    section = _owner_section(_guide())
    for reason in (
        "writer_live",
        "checkout_in_use",
        "origin_unreachable",
        "stale_fence",
        "not_found",
        "not_recoverable_state",
    ):
        assert reason in section, f"the guide does not cover the refusal {reason!r}"


def test_guide_says_where_preserved_work_lands():
    section = _owner_section(_guide())
    assert "aq/preserved/" in section


def test_guide_points_at_the_stranded_fences_doctor_fix():
    section = _owner_section(_guide())
    assert "integration.stranded_fences" in section
    assert "--fix" in section


def test_guide_shows_the_audit_table_and_a_psql_query():
    text = _guide()
    section = _owner_section(text)
    assert "integration_owner_recoveries" in section
    assert "psql" in section


def test_owner_section_no_longer_says_LOCAL_operator():
    section = _owner_section(_guide())
    assert "LOCAL operator" not in section
    assert "LOCAL authority" not in section


def test_related_pages_and_source_listed():
    text = _guide()
    assert "owner_recovery" in text
