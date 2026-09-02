"""Package 0 T-4 — ``ExecutionPrincipal`` is server-derived, never supplied.

The principal is built inside ``CommandHandler.execute`` from the
server-derived ``_scope``, and fails closed at every step where identity
cannot be resolved.  A request body cannot populate or widen it.

Committed first (roadmap commit 1) with ``xfail(strict=True)``; T-11
removes the markers.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.models import AgentProfile, Project, SessionRecord

pytestmark = pytest.mark.asyncio


async def _seed(handler, *, profile: AgentProfile | None, session_id: str = "s1"):
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    if profile is not None:
        await db.create_profile(profile)
    await db.create_session(
        SessionRecord(
            id=session_id,
            task_id=None,
            project_id="p",
            agent_id=None,
            profile_id=(profile.id if profile is not None else ""),
            harness="claude",
            provider="fake",
            name=session_id,
            lifecycle="task",
            work_dir="/tmp",
            epoch="e1",
            instance_token="t1",
            started_at=0.0,
            state="running",
        )
    )
    return db


def _scope(session_id: str = "s1", **kw) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "task_id": None,
        "project_id": "p",
        "elevated": False,
        **kw,
    }


class TestPrincipalType:
    async def test_trusted_local_is_not_enforced(self):
        from src.commands.principal import TRUSTED_LOCAL, PrincipalKind

        assert TRUSTED_LOCAL.kind is PrincipalKind.LOCAL
        assert TRUSTED_LOCAL.enforced is False

    async def test_service_principal_is_not_enforced(self):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind

        p = ExecutionPrincipal.service("cascade")
        assert p.kind is PrincipalKind.SERVICE
        assert p.service_name == "cascade"
        assert p.enforced is False

    async def test_session_and_playbook_principals_are_enforced(self):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import DENY_ALL

        for kind in (PrincipalKind.SESSION, PrincipalKind.PLAYBOOK):
            assert ExecutionPrincipal(kind=kind, policy=DENY_ALL).enforced is True

    async def test_frozen(self):
        from src.commands.principal import TRUSTED_LOCAL

        with pytest.raises(dataclasses.FrozenInstanceError):
            TRUSTED_LOCAL.profile_id = "supervisor"  # type: ignore[misc]

    async def test_narrow_never_widens(self):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        narrow = CapabilityPolicy.from_namespaces(aq_commands=["task_close"])
        broad = CapabilityPolicy.from_namespaces(
            aq_commands=["task_close", "create_task"], harness_tools=["Bash"]
        )
        p = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=narrow)

        widened = p.narrow(broad, reason="step-override")

        assert widened.policy == narrow
        assert widened.provenance == ("step-override",)

    async def test_provenance_accumulates_one_entry_per_narrowing(self):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        p = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["a", "b", "c"]),
        )
        p = p.narrow(CapabilityPolicy.from_namespaces(aq_commands=["a", "b"]), reason="one")
        p = p.narrow(CapabilityPolicy.from_namespaces(aq_commands=["a"]), reason="two")

        assert p.provenance == ("one", "two")
        assert p.policy.aq_commands == frozenset({"a"})

    async def test_there_is_no_widening_method(self):
        from src.commands.principal import ExecutionPrincipal

        assert not hasattr(ExecutionPrincipal, "widen")
        assert not hasattr(ExecutionPrincipal, "grant")


class TestPrincipalContext:
    async def test_context_var_restores_on_exit(self):
        from src.commands.principal import (
            ExecutionPrincipal,
            PrincipalKind,
            current_principal,
            principal_context,
        )
        from src.profiles.capabilities import DENY_ALL

        outer = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="a")
        inner = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="b")

        assert current_principal() is None
        with principal_context(outer):
            assert current_principal() is outer
            with principal_context(inner):
                assert current_principal() is inner
            assert current_principal() is outer
        assert current_principal() is None


class TestSeamDerivation:
    async def test_no_scope_yields_the_trusted_local_principal(self, command_handler_factory):
        from src.commands.principal import PrincipalKind

        handler = await command_handler_factory()
        p = await handler._principal_from_scope(None)
        assert p.kind is PrincipalKind.LOCAL
        assert p.enforced is False

    async def test_local_scope_yields_the_trusted_local_principal(self, command_handler_factory):
        from src.commands.principal import PrincipalKind

        handler = await command_handler_factory()
        p = await handler._principal_from_scope({"kind": "local"})
        assert p.kind is PrincipalKind.LOCAL

    async def test_live_session_row_yields_the_profiles_policy(self, command_handler_factory):
        from src.commands.principal import PrincipalKind

        handler = await command_handler_factory()
        await _seed(
            handler,
            profile=AgentProfile(
                id="narrow",
                name="narrow",
                harness_tools=["Bash"],
                aq_commands=["task_close"],
                plugin_tools=[],
            ),
        )

        p = await handler._principal_from_scope(_scope())

        assert p.kind is PrincipalKind.SESSION
        assert p.profile_id == "narrow"
        assert p.policy.aq_commands == frozenset({"task_close"})
        assert p.enforced is True

    async def test_missing_session_row_fails_closed(self, command_handler_factory):
        from src.profiles.capabilities import DENY_ALL

        handler = await command_handler_factory()
        p = await handler._principal_from_scope(_scope("ghost"))
        assert p.policy == DENY_ALL
        assert p.provenance == ("session-not-found",)

    async def test_session_with_no_profile_fails_closed(self, command_handler_factory):
        from src.profiles.capabilities import DENY_ALL

        handler = await command_handler_factory()
        await _seed(handler, profile=None)
        p = await handler._principal_from_scope(_scope())
        assert p.policy == DENY_ALL
        assert p.provenance == ("session-has-no-profile",)

    async def test_deleted_profile_fails_closed(self, command_handler_factory):
        from src.profiles.capabilities import DENY_ALL

        handler = await command_handler_factory()
        await _seed(handler, profile=AgentProfile(id="gone", name="gone"))
        await handler.db.delete_profile("gone")
        handler._invalidate_principal_cache()

        p = await handler._principal_from_scope(_scope())

        assert p.policy == DENY_ALL
        assert p.provenance == ("profile-not-found",)

    async def test_an_unusable_store_fails_closed_rather_than_raising(
        self, command_handler_factory
    ):
        """A store that cannot answer must deny, not blow up the dispatch.

        ``execute`` is the single dispatch seam for Discord, MCP, the CLI and
        both HTTP surfaces.  Raising out of the seam turns an unresolvable
        identity into a 500 on *every* session-scoped command instead of a
        clean denial — worse than the fail-closed answer, and it hides the
        cause.  Reached whenever the handler has no live database: early
        startup before ``Database.initialize``, and any caller holding a stub
        orchestrator.
        """
        from src.profiles.capabilities import DENY_ALL

        handler = await command_handler_factory()
        handler.orchestrator.db = None
        handler._invalidate_principal_cache()

        p = await handler._principal_from_scope(_scope())

        assert p.policy == DENY_ALL
        assert p.provenance == ("database-unavailable",)
        assert p.unresolved is True

    async def test_a_raising_store_fails_closed(self, command_handler_factory):
        """Same answer when the lookup itself errors, not just when it is absent."""
        from src.profiles.capabilities import DENY_ALL

        handler = await command_handler_factory()
        await _seed(handler, profile=AgentProfile(id="narrow", name="narrow"))
        handler._invalidate_principal_cache()

        async def _boom(_session_id):
            raise RuntimeError("connection pool exhausted")

        handler.orchestrator.db.get_session = _boom

        p = await handler._principal_from_scope(_scope())

        assert p.policy == DENY_ALL
        assert p.provenance == ("database-unavailable",)

    async def test_elevated_scope_still_carries_the_profile_policy(self, command_handler_factory):
        handler = await command_handler_factory()
        await _seed(
            handler,
            profile=AgentProfile(
                id="sup", name="sup", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )

        p = await handler._principal_from_scope(_scope(elevated=True))

        assert p.elevated is True
        assert p.policy.aq_commands == frozenset({"task_close"})


class TestClientSuppliedFieldsAreStripped:
    async def test_server_owned_arg_keys_never_reach_a_handler(self, command_handler_factory):
        handler = await command_handler_factory()
        seen: dict = {}

        async def _cmd_probe(args: dict) -> dict:
            seen.update(args)
            return {"success": True}

        handler._cmd_probe = _cmd_probe  # type: ignore[attr-defined]

        await handler.execute(
            "probe",
            {
                "keep": 1,
                "_principal": {"kind": "local"},
                "_policy": {"aq_commands": ["*"]},
                "_profile_id": "supervisor",
                "_capabilities": {"aq_commands": ["*"]},
            },
        )

        assert seen == {"keep": 1}

    async def test_spoofed_policy_does_not_change_the_derived_principal(
        self, command_handler_factory
    ):
        from src.commands.principal import current_principal

        handler = await command_handler_factory()
        await _seed(
            handler,
            profile=AgentProfile(
                id="narrow", name="narrow", harness_tools=["Bash"],
                aq_commands=["probe"], plugin_tools=[],
            ),
        )
        captured: list = []

        async def _cmd_probe(args: dict) -> dict:
            captured.append(current_principal())
            return {"success": True}

        handler._cmd_probe = _cmd_probe  # type: ignore[attr-defined]

        await handler.execute(
            "probe",
            {
                "_scope": _scope(),
                "_policy": {"aq_commands": ["everything"]},
                "_profile_id": "supervisor",
            },
        )

        assert captured[0].profile_id == "narrow"
        assert captured[0].policy.aq_commands == frozenset({"probe"})

    async def test_server_owned_keys_are_stripped_at_the_http_surface(self):
        from src.api.execute import _SERVER_OWNED_ARG_KEYS

        assert set(_SERVER_OWNED_ARG_KEYS) == {
            "_scope", "_principal", "_policy", "_profile_id", "_capabilities",
        }


class TestReentrantExecute:
    async def test_inner_execute_restores_the_outer_principal(self, command_handler_factory):
        from src.commands.principal import current_principal

        handler = await command_handler_factory()
        await _seed(
            handler,
            profile=AgentProfile(
                id="narrow", name="narrow", harness_tools=["Bash"],
                aq_commands=["outer", "inner"], plugin_tools=[],
            ),
        )
        observed: list = []

        async def _cmd_inner(args: dict) -> dict:
            observed.append(("inner", current_principal().kind))
            return {"success": True}

        async def _cmd_outer(args: dict) -> dict:
            before = current_principal()
            await handler.execute("inner", {})
            after = current_principal()
            observed.append(("outer", before is after))
            return {"success": True}

        handler._cmd_inner = _cmd_inner  # type: ignore[attr-defined]
        handler._cmd_outer = _cmd_outer  # type: ignore[attr-defined]

        await handler.execute("outer", {"_scope": _scope()})

        assert observed[-1] == ("outer", True)


class TestRequestScopeFields:
    async def test_request_scope_carries_server_derived_identity(self):
        from src.api.auth import RequestScope

        s = RequestScope(kind="session", session_id="s1")
        assert s.profile_id is None
        assert s.policy_fingerprint is None
        s2 = RequestScope(
            kind="session", session_id="s1", profile_id="narrow", policy_fingerprint="sha256:x"
        )
        assert s2.profile_id == "narrow"
        assert s2.policy_fingerprint == "sha256:x"
