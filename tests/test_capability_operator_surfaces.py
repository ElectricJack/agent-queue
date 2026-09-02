"""Package 0 T-19 — the operator surfaces for capability migration.

Package 0 ships with enforcement in ``audit``, so an un-migrated fleet keeps
running.  That is only safe if an operator can see what still needs
migrating, which is what ``aq profile audit`` and the ``capability.*`` doctor
checks are for.
"""

from __future__ import annotations

import pytest

from src.doctor.capability_checks import capability_checks
from src.doctor.models import DoctorContext, Severity
from src.models import AgentProfile

pytestmark = pytest.mark.asyncio

_BY_ID = {c.id: c for c in capability_checks()}


async def _run(check_id: str, *, db=None, config=None):
    return await _BY_ID[check_id].run(DoctorContext(db=db, config=config))


class TestProfileAudit:
    async def test_reports_source_namespaces_and_fingerprint(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(id="legacy", name="legacy", allowed_tools=["Bash", "task_close"])
        )
        await handler.db.create_profile(
            AgentProfile(
                id="explicit", name="explicit",
                harness_tools=["Bash"], aq_commands=["task_close"], plugin_tools=[],
            )
        )

        result = await handler.execute("profile_audit", {})

        assert result["success"] is True
        rows = {r["id"]: r for r in result["profiles"]}
        assert rows["legacy"]["source"] == "legacy"
        assert rows["explicit"]["source"] == "explicit"
        assert rows["explicit"]["harness_tools"] == ["Bash"]
        assert rows["explicit"]["aq_commands"] == ["task_close"]
        assert rows["explicit"]["fingerprint"].startswith("sha256:")
        assert result["legacy_count"] == 1

    async def test_legacy_rows_sort_first(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(
                id="a-explicit", name="a", harness_tools=["Bash"], aq_commands=[], plugin_tools=[]
            )
        )
        await handler.db.create_profile(
            AgentProfile(id="z-legacy", name="z", allowed_tools=["Bash"])
        )

        result = await handler.execute("profile_audit", {})

        assert [r["id"] for r in result["profiles"]] == ["z-legacy", "a-explicit"]

    async def test_legacy_only_filters(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(
                id="explicit", name="e", harness_tools=["Bash"], aq_commands=[], plugin_tools=[]
            )
        )
        await handler.db.create_profile(
            AgentProfile(id="legacy", name="l", allowed_tools=["Bash"])
        )

        result = await handler.execute("profile_audit", {"legacy_only": True})

        assert [r["id"] for r in result["profiles"]] == ["legacy"]

    async def test_a_stored_wildcard_is_reported_not_raised(self, command_handler_factory):
        """The audit must survive the one state it exists to find."""
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(id="bad", name="bad", allowed_tools=["*"])
        )

        result = await handler.execute("profile_audit", {})

        row = next(r for r in result["profiles"] if r["id"] == "bad")
        assert "wildcard" in row["error"]


class TestDoctorChecks:
    async def test_enforcement_warns_until_enforce(self, command_handler_factory):
        handler = await command_handler_factory()

        handler.config.security.capability_enforcement = "audit"
        assert (await _run("capability.enforcement", config=handler.config)).severity is (
            Severity.WARN
        )

        handler.config.security.capability_enforcement = "enforce"
        assert (await _run("capability.enforcement", config=handler.config)).severity is (
            Severity.OK
        )

    async def test_legacy_profiles_warn_and_name_the_migration_list(
        self, command_handler_factory
    ):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(id="legacy", name="l", allowed_tools=["Bash"])
        )

        result = await _run("capability.legacy_profiles", db=handler.db)

        assert result.severity is Severity.WARN
        assert result.data["profiles"] == ["legacy"]

    async def test_legacy_profiles_ok_when_all_migrated(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(
                id="explicit", name="e", harness_tools=["Bash"], aq_commands=[], plugin_tools=[]
            )
        )

        assert (await _run("capability.legacy_profiles", db=handler.db)).severity is Severity.OK

    async def test_a_stored_wildcard_is_an_error(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(id="bad", name="bad", allowed_tools=["mcp__github__*"])
        )

        result = await _run("capability.wildcards", db=handler.db)

        assert result.severity is Severity.ERROR
        assert "bad" in result.data["profiles"]

    async def test_no_wildcards_is_ok(self, command_handler_factory):
        handler = await command_handler_factory()
        await handler.db.create_profile(
            AgentProfile(id="fine", name="fine", allowed_tools=["Bash"])
        )

        assert (await _run("capability.wildcards", db=handler.db)).severity is Severity.OK

    async def test_no_check_offers_a_fix(self):
        """All three are report-only: these are operator decisions."""
        assert all(c.fix is None for c in capability_checks())
