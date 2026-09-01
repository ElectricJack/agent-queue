"""Package 0 T-3/T-12/T-15 — capability authorization at the dispatch boundary.

``CommandHandler.execute`` is the single dispatch seam for Discord, MCP,
the CLI and both HTTP surfaces.  Before Package 0 nothing there consulted
the caller's profile: the only gate was ``check_request_scope``, which is
identical for every non-elevated session.  Package 0 adds a second,
independent gate that runs *before* the built-in handler lookup, so the
plugin fallback is covered by the same check.

Committed first (roadmap commit 1) with ``xfail(strict=True)``; T-13
removes the markers.
"""

from __future__ import annotations

import pytest

from src.models import AgentProfile, Project, SessionRecord

#: The xfail marker is removed by T-13 (roadmap commit 4).
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.xfail(strict=True, reason="Package 0 T-3"),
]


def _scope(session_id: str = "s1", **kw) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "task_id": None,
        "project_id": "p",
        "elevated": False,
        **kw,
    }


async def _seed_session(handler, profile: AgentProfile, session_id: str = "s1"):
    db = handler.db
    if await db.get_project("p") is None:
        await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(profile)
    await db.create_session(
        SessionRecord(
            id=session_id,
            task_id=None,
            project_id="p",
            agent_id=None,
            profile_id=profile.id,
            harness="claude",
            provider="fake",
            name=session_id,
            lifecycle="task",
            state="running",
        )
    )
    handler._invalidate_principal_cache()


@pytest.fixture
async def enforcing_handler(internal_plugins_handler):
    handler = await internal_plugins_handler()
    handler.config.security.capability_enforcement = "enforce"
    return handler


# ---------------------------------------------------------------------------
# Pure policy / resolver layer (T-12)
# ---------------------------------------------------------------------------


class TestPolicyLayer:
    async def test_command_allowed_uses_the_aq_namespace_for_builtins(self):
        from src.commands.authorization import command_allowed
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        class _Resolver:
            def is_builtin(self, name: str) -> bool:
                return name == "list_tasks"

            def is_plugin(self, name: str) -> bool:
                return False

            def plugin_command_names(self):
                return frozenset()

        p = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["list_tasks"]),
        )
        assert command_allowed("list_tasks", p, resolver=_Resolver()) is True
        assert command_allowed("delete_task", p, resolver=_Resolver()) is False

    async def test_command_allowed_uses_the_plugin_namespace_for_plugins(self):
        from src.commands.authorization import command_allowed
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        class _Resolver:
            def is_builtin(self, name: str) -> bool:
                return False

            def is_plugin(self, name: str) -> bool:
                return name == "read_file"

            def plugin_command_names(self):
                return frozenset({"read_file"})

        allowed = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(plugin_tools=["read_file"]),
        )
        denied = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["read_file"]),
        )
        assert command_allowed("read_file", allowed, resolver=_Resolver()) is True
        assert command_allowed("read_file", denied, resolver=_Resolver()) is False

    async def test_trusted_principals_bypass(self):
        from src.commands.authorization import command_allowed
        from src.commands.principal import TRUSTED_LOCAL, ExecutionPrincipal

        class _Resolver:
            def is_builtin(self, name: str) -> bool:
                return True

            def is_plugin(self, name: str) -> bool:
                return False

            def plugin_command_names(self):
                return frozenset()

        assert command_allowed("anything", TRUSTED_LOCAL, resolver=_Resolver()) is True
        assert (
            command_allowed("anything", ExecutionPrincipal.service("cascade"), resolver=_Resolver())
            is True
        )

    async def test_authorize_command_reports_namespace_and_shadow(self):
        from src.commands.authorization import authorize_command
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        class _Resolver:
            def is_builtin(self, name: str) -> bool:
                return True

            def is_plugin(self, name: str) -> bool:
                return False

            def plugin_command_names(self):
                return frozenset()

        legacy = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(
                aq_commands=["x"], derived_from_legacy=True
            ),
        )
        explicit = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["x"]),
        )

        d = authorize_command("y", legacy, resolver=_Resolver(), mode="audit")
        assert d.allowed is False and d.shadow is True and d.namespace == "aq_commands"

        d = authorize_command("y", explicit, resolver=_Resolver(), mode="audit")
        assert d.allowed is False and d.shadow is False

        d = authorize_command("y", legacy, resolver=_Resolver(), mode="enforce")
        assert d.allowed is False and d.shadow is False

        d = authorize_command("y", explicit, resolver=_Resolver(), mode="off")
        assert d.allowed is True and d.shadow is False


# ---------------------------------------------------------------------------
# Real dispatch (T-13)
# ---------------------------------------------------------------------------


class TestDispatchEnforcement:
    async def test_off_list_builtin_is_denied(self, enforcing_handler):
        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="narrow", name="narrow", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )

        result = await enforcing_handler.execute("list_tasks", {"_scope": _scope()})

        assert result.get("error_code") == "capability_denied"
        assert result["error"] == "capability denied: list_tasks"

    async def test_on_list_builtin_is_allowed(self, enforcing_handler):
        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="lister", name="lister", harness_tools=["Bash"],
                aq_commands=["list_tasks"], plugin_tools=[],
            ),
        )

        result = await enforcing_handler.execute("list_tasks", {"_scope": _scope()})

        assert result.get("error_code") != "capability_denied"

    async def test_off_list_plugin_is_denied_before_the_handler_runs(self, enforcing_handler):
        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="narrow", name="narrow", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )
        registry = enforcing_handler.orchestrator.plugin_registry
        called: list = []

        async def _boom(args: dict) -> dict:
            called.append(args)
            raise AssertionError("plugin handler must not run for a denied command")

        original = registry.get_command
        registry.get_command = lambda name: _boom if name == "read_file" else original(name)

        result = await enforcing_handler.execute("read_file", {"_scope": _scope()})

        assert result.get("error_code") == "capability_denied"
        assert called == []

    async def test_on_list_plugin_is_allowed(self, enforcing_handler):
        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="reader", name="reader", harness_tools=["Bash"],
                aq_commands=[], plugin_tools=["read_file"],
            ),
        )

        result = await enforcing_handler.execute(
            "read_file", {"_scope": _scope(), "path": "/nonexistent-file"}
        )

        assert result.get("error_code") != "capability_denied"

    async def test_deny_all_principal_is_denied_for_every_builtin(self, enforcing_handler):
        from src.tools.registry import _builtin_command_names

        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="none", name="none", harness_tools=[], aq_commands=[], plugin_tools=[]
            ),
        )

        for name in sorted(_builtin_command_names())[:25]:
            result = await enforcing_handler.execute(name, {"_scope": _scope()})
            assert result.get("error_code") == "capability_denied", name

    async def test_trusted_local_is_allowed(self, enforcing_handler):
        result = await enforcing_handler.execute("list_tasks", {})
        assert result.get("error_code") != "capability_denied"

    async def test_service_principal_is_allowed(self, enforcing_handler):
        from src.commands.principal import ExecutionPrincipal, principal_context

        with principal_context(ExecutionPrincipal.service("cascade")):
            result = await enforcing_handler.execute("list_tasks", {})
        assert result.get("error_code") != "capability_denied"

    async def test_elevated_session_is_still_subject_to_its_policy(self, enforcing_handler):
        await _seed_session(
            enforcing_handler,
            AgentProfile(
                id="sup", name="sup", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )

        result = await enforcing_handler.execute(
            "list_tasks", {"_scope": _scope(elevated=True)}
        )

        assert result.get("error_code") == "capability_denied"


class TestEnforcementModes:
    async def test_audit_allows_a_legacy_derived_policy_with_a_shadow_warning(
        self, internal_plugins_handler, caplog
    ):
        handler = await internal_plugins_handler()
        handler.config.security.capability_enforcement = "audit"
        await _seed_session(
            handler,
            AgentProfile(id="legacy", name="legacy", allowed_tools=["Bash", "task_close"]),
        )

        with caplog.at_level("WARNING"):
            result = await handler.execute("list_tasks", {"_scope": _scope()})

        assert result.get("error_code") != "capability_denied"
        assert "capability_denied_shadow" in caplog.text

    async def test_audit_denies_an_explicitly_authored_policy(self, internal_plugins_handler):
        handler = await internal_plugins_handler()
        handler.config.security.capability_enforcement = "audit"
        await _seed_session(
            handler,
            AgentProfile(
                id="explicit", name="explicit", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )

        result = await handler.execute("list_tasks", {"_scope": _scope()})

        assert result.get("error_code") == "capability_denied"

    async def test_off_allows_everything(self, internal_plugins_handler):
        handler = await internal_plugins_handler()
        handler.config.security.capability_enforcement = "off"
        await _seed_session(
            handler,
            AgentProfile(
                id="explicit", name="explicit", harness_tools=["Bash"],
                aq_commands=["task_close"], plugin_tools=[],
            ),
        )

        result = await handler.execute("list_tasks", {"_scope": _scope()})

        assert result.get("error_code") != "capability_denied"

    async def test_config_rejects_an_unknown_mode(self):
        from src.config import SecurityConfig

        errors = SecurityConfig(capability_enforcement="loud").validate()
        assert any(e.field == "capability_enforcement" for e in errors)
        assert SecurityConfig(capability_enforcement="enforce").validate() == []


# ---------------------------------------------------------------------------
# Discovery/dispatch parity (§4.3, T-15)
# ---------------------------------------------------------------------------


class TestDiscoveryDispatchParity:
    async def test_filter_tool_definitions_matches_command_allowed(self):
        from src.commands.authorization import command_allowed, filter_tool_definitions
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        class _Resolver:
            def is_builtin(self, name: str) -> bool:
                return name in {"list_tasks", "task_close"}

            def is_plugin(self, name: str) -> bool:
                return name == "read_file"

            def plugin_command_names(self):
                return frozenset({"read_file"})

        defs = [{"name": n} for n in ("list_tasks", "task_close", "read_file")]
        principal = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(
                aq_commands=["task_close"], plugin_tools=["read_file"]
            ),
        )

        kept = {d["name"] for d in filter_tool_definitions(defs, principal, resolver=_Resolver())}

        assert kept == {"task_close", "read_file"}
        for d in defs:
            assert (d["name"] in kept) == command_allowed(
                d["name"], principal, resolver=_Resolver()
            )

    async def test_published_names_equal_runnable_names(self, enforcing_handler):
        """One predicate: a published name is runnable, a denied name is not."""
        from src.commands.authorization import command_allowed
        from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
        from src.profiles.capabilities import CapabilityPolicy, DENY_ALL
        from src.tools.registry import _builtin_command_names

        resolver = enforcing_handler._command_resolver
        names = sorted(_builtin_command_names())[:15]
        principals = [
            ExecutionPrincipal(
                kind=PrincipalKind.SESSION,
                policy=CapabilityPolicy.from_namespaces(aq_commands=names[:5]),
            ),
            ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL),
        ]

        for principal in principals:
            with principal_context(principal):
                for name in names:
                    published = command_allowed(name, principal, resolver=resolver)
                    result = await enforcing_handler.execute(name, {})
                    ran = result.get("error_code") != "capability_denied"
                    assert published == ran, (name, principal.policy.aq_commands)

    async def test_node_tools_denies_when_no_policy_is_declared(self):
        from src.llm import LLMClient

        from src.playbooks.services import PlaybookServices

        services = PlaybookServices.for_tests(LLMClient.__new__(LLMClient))
        services.tool_registry.get_all_tools.return_value = [{"name": "task_close"}]

        assert services.node_tools(None) == []

    async def test_node_tools_filters_unknown_names_instead_of_raising(self):
        from src.llm import LLMClient
        from src.playbooks.services import PlaybookServices

        services = PlaybookServices.for_tests(LLMClient.__new__(LLMClient))
        services.tool_registry.get_all_tools.return_value = [
            {"name": "task_close"},
            {"name": "delete_task"},
        ]

        assert services.node_tools(["task_close", "nonexistent"]) == [{"name": "task_close"}]
