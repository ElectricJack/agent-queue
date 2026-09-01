"""Package 0 T-1 — ``CapabilityPolicy`` invariants.

The three-namespace policy is the locked cross-package interface of the
Playbook V2 roadmap §4: exact membership, intersection, subset checks,
canonical serialization, a deny-by-default empty value, and no wildcards
in any namespace.

Committed first (roadmap commit 1) with ``xfail(strict=True)`` so the
assertion is proven to fail before ``src/profiles/capabilities.py``
exists; T-8 removes the markers in the same commit that implements it.
"""

from __future__ import annotations

import pytest

#: Removed by T-8 (roadmap commit 3), which implements the module under test.
pytestmark = pytest.mark.xfail(strict=True, reason="Package 0 T-1")


class TestConstruction:
    def test_empty_policy_denies_every_namespace(self):
        from src.profiles.capabilities import CapabilityPolicy

        p = CapabilityPolicy()
        assert p.allows_aq_command("task_close") is False
        assert p.allows_harness_tool("Bash") is False
        assert p.allows_plugin_tool("read_file") is False

    def test_deny_all_constant_is_empty(self):
        from src.profiles.capabilities import DENY_ALL

        assert DENY_ALL.harness_tools == frozenset()
        assert DENY_ALL.aq_commands == frozenset()
        assert DENY_ALL.plugin_tools == frozenset()
        assert DENY_ALL.derived_from_legacy is False

    def test_from_namespaces_builds_frozensets(self):
        from src.profiles.capabilities import CapabilityPolicy

        p = CapabilityPolicy.from_namespaces(
            harness_tools=["Bash", "Read"],
            aq_commands=["task_close"],
            plugin_tools=["read_file"],
        )
        assert p.harness_tools == frozenset({"Bash", "Read"})
        assert p.aq_commands == frozenset({"task_close"})
        assert p.plugin_tools == frozenset({"read_file"})
        assert p.allows("harness_tools", "Bash") is True
        assert p.allows("aq_commands", "Bash") is False

    def test_membership_is_exact_no_prefix_or_case_folding(self):
        from src.profiles.capabilities import CapabilityPolicy

        p = CapabilityPolicy.from_namespaces(aq_commands=["task_close"])
        assert p.allows_aq_command("task_close") is True
        assert p.allows_aq_command("task_clos") is False
        assert p.allows_aq_command("task_close_extra") is False
        assert p.allows_aq_command("TASK_CLOSE") is False

    def test_frozen(self):
        import dataclasses

        from src.profiles.capabilities import CapabilityPolicy

        p = CapabilityPolicy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.harness_tools = frozenset({"Bash"})  # type: ignore[misc]


class TestWildcardRejection:
    @pytest.mark.parametrize("bad", ["*", "**", "mcp__github__*", "task_?lose"])
    def test_wildcard_entries_rejected(self, bad):
        from src.profiles.capabilities import CapabilityPolicyError, CapabilityPolicy

        with pytest.raises(CapabilityPolicyError) as exc:
            CapabilityPolicy.from_namespaces(harness_tools=[bad])
        assert "wildcard" in str(exc.value)

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_entries_rejected(self, bad):
        from src.profiles.capabilities import CapabilityPolicyError, CapabilityPolicy

        with pytest.raises(CapabilityPolicyError):
            CapabilityPolicy.from_namespaces(aq_commands=[bad])

    def test_non_string_entry_rejected(self):
        from src.profiles.capabilities import CapabilityPolicyError, CapabilityPolicy

        with pytest.raises(CapabilityPolicyError):
            CapabilityPolicy.from_namespaces(plugin_tools=[123])  # type: ignore[list-item]

    def test_wildcard_rejected_in_every_namespace(self):
        from src.profiles.capabilities import CapabilityPolicyError, CapabilityPolicy

        for ns in ("harness_tools", "aq_commands", "plugin_tools"):
            with pytest.raises(CapabilityPolicyError):
                CapabilityPolicy.from_namespaces(**{ns: ["*"]})


class TestIntersect:
    def test_intersect_is_per_namespace(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(
            harness_tools=["Bash", "Read"], aq_commands=["x", "y"]
        )
        b = CapabilityPolicy.from_namespaces(harness_tools=["Bash"], aq_commands=["y", "z"])
        c = a.intersect(b)
        assert c.harness_tools == frozenset({"Bash"})
        assert c.aq_commands == frozenset({"y"})
        assert c.plugin_tools == frozenset()

    def test_intersect_does_not_leak_across_namespaces(self):
        from src.profiles.capabilities import CapabilityPolicy, DENY_ALL

        a = CapabilityPolicy.from_namespaces(aq_commands=["x"])
        b = CapabilityPolicy.from_namespaces(harness_tools=["x"])
        assert a.intersect(b) == DENY_ALL

    def test_intersect_propagates_derived_from_legacy(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(aq_commands=["x"], derived_from_legacy=True)
        b = CapabilityPolicy.from_namespaces(aq_commands=["x"])
        assert a.intersect(b).derived_from_legacy is True
        assert b.intersect(a).derived_from_legacy is True
        assert b.intersect(b).derived_from_legacy is False


class TestSubset:
    def test_equality_is_a_subset(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(harness_tools=["Bash"], aq_commands=["x"])
        b = CapabilityPolicy.from_namespaces(harness_tools=["Bash"], aq_commands=["x"])
        assert a.is_subset_of(b) is True

    def test_deny_all_is_a_subset_of_anything(self):
        from src.profiles.capabilities import CapabilityPolicy, DENY_ALL

        broad = CapabilityPolicy.from_namespaces(
            harness_tools=["Bash"], aq_commands=["x"], plugin_tools=["read_file"]
        )
        assert DENY_ALL.is_subset_of(broad) is True
        assert broad.is_subset_of(DENY_ALL) is False

    @pytest.mark.parametrize("ns", ["harness_tools", "aq_commands", "plugin_tools"])
    def test_one_extra_name_in_any_namespace_breaks_subset(self, ns):
        from src.profiles.capabilities import CapabilityPolicy

        parent = CapabilityPolicy.from_namespaces(
            harness_tools=["Bash"], aq_commands=["x"], plugin_tools=["read_file"]
        )
        kwargs = {
            "harness_tools": ["Bash"],
            "aq_commands": ["x"],
            "plugin_tools": ["read_file"],
        }
        kwargs[ns] = [*kwargs[ns], "extra_name"]
        child = CapabilityPolicy.from_namespaces(**kwargs)
        assert child.is_subset_of(parent) is False


class TestCanonicalAndFingerprint:
    def test_to_canonical_is_sorted(self):
        from src.profiles.capabilities import CapabilityPolicy

        p = CapabilityPolicy.from_namespaces(harness_tools=["Read", "Bash", "Glob"])
        assert p.to_canonical() == {
            "harness_tools": ["Bash", "Glob", "Read"],
            "aq_commands": [],
            "plugin_tools": [],
        }

    def test_fingerprint_is_order_independent(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(harness_tools=["Read", "Bash"])
        b = CapabilityPolicy.from_namespaces(harness_tools=["Bash", "Read"])
        assert a.fingerprint() == b.fingerprint()
        assert a.fingerprint().startswith("sha256:")

    def test_fingerprint_changes_when_a_name_changes(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(aq_commands=["task_close"])
        b = CapabilityPolicy.from_namespaces(aq_commands=["task_open"])
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_ignores_derived_from_legacy(self):
        from src.profiles.capabilities import CapabilityPolicy

        a = CapabilityPolicy.from_namespaces(aq_commands=["x"], derived_from_legacy=True)
        b = CapabilityPolicy.from_namespaces(aq_commands=["x"], derived_from_legacy=False)
        assert a.fingerprint() == b.fingerprint()


class TestClassifier:
    def test_harness_tool(self):
        from src.profiles.capabilities import classify_capability

        assert classify_capability("Bash") == "harness_tools"
        assert classify_capability("NotebookEdit") == "harness_tools"

    def test_mcp_prefixed_names_are_plugin_tools(self):
        from src.profiles.capabilities import classify_capability

        assert classify_capability("mcp__github__create_issue") == "plugin_tools"

    def test_registered_plugin_command_is_a_plugin_tool(self):
        from src.profiles.capabilities import classify_capability

        assert (
            classify_capability("read_file", plugin_command_names=frozenset({"read_file"}))
            == "plugin_tools"
        )

    def test_unknown_names_default_to_aq_commands(self):
        from src.profiles.capabilities import classify_capability

        assert classify_capability("task_close") == "aq_commands"
        # No registry wired: the stricter reading, since aq_commands is
        # checked against a registry-derived set and an unknown plugin
        # name is denied rather than silently allowed.
        assert classify_capability("read_file") == "aq_commands"

    def test_harness_tool_names_has_one_definition(self):
        from src.profiles.capabilities import HARNESS_TOOL_NAMES
        from src.sessions.spec import SessionSpecBuilder

        assert SessionSpecBuilder._HARNESS_TOOL_NAMES is HARNESS_TOOL_NAMES


class TestLegacyAdapter:
    def test_legacy_profile_splits_into_namespaces(self):
        from src.models import AgentProfile
        from src.profiles.capabilities import capability_policy_for

        profile = AgentProfile(
            id="legacy",
            name="legacy",
            allowed_tools=[
                "Bash", "Read", "Write", "Edit", "Glob", "Grep",
                "get_task", "task_close", "reopen_with_feedback",
            ],
        )
        p = capability_policy_for(profile)
        assert p.harness_tools == frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})
        assert p.aq_commands == frozenset({"get_task", "task_close", "reopen_with_feedback"})
        assert p.plugin_tools == frozenset()
        assert p.derived_from_legacy is True

    def test_r1_empty_allowed_tools_yields_the_harness_names(self):
        from src.models import AgentProfile
        from src.profiles.capabilities import (
            AGENT_COMMAND_FALLBACK,
            HARNESS_TOOL_NAMES,
            capability_policy_for,
        )

        p = capability_policy_for(AgentProfile(id="e", name="e", allowed_tools=[]))
        assert p.harness_tools == HARNESS_TOOL_NAMES
        assert p.aq_commands == AGENT_COMMAND_FALLBACK
        assert p.derived_from_legacy is True

    def test_r2_no_aq_names_declared_yields_the_agent_command_set(self):
        from src.api.scope import AGENT_COMMAND_SET
        from src.models import AgentProfile
        from src.profiles.capabilities import capability_policy_for

        p = capability_policy_for(
            AgentProfile(id="h", name="h", allowed_tools=["Bash", "Read"])
        )
        assert p.harness_tools == frozenset({"Bash", "Read"})
        assert p.aq_commands == AGENT_COMMAND_SET
        assert p.derived_from_legacy is True

    def test_explicit_namespaces_are_not_legacy(self):
        from src.models import AgentProfile
        from src.profiles.capabilities import capability_policy_for

        profile = AgentProfile(
            id="x",
            name="x",
            allowed_tools=["Bash"],
            harness_tools=["Bash", "Read"],
            aq_commands=["task_close"],
            plugin_tools=[],
        )
        p = capability_policy_for(profile)
        assert p.derived_from_legacy is False
        assert p.harness_tools == frozenset({"Bash", "Read"})
        assert p.aq_commands == frozenset({"task_close"})
        # ``allowed_tools`` is ignored entirely once namespaces are authored.
        assert p.plugin_tools == frozenset()

    def test_explicitly_empty_namespaces_deny_everything(self):
        from src.models import AgentProfile
        from src.profiles.capabilities import DENY_ALL, capability_policy_for

        profile = AgentProfile(
            id="x",
            name="x",
            allowed_tools=["Bash", "task_close"],
            harness_tools=[],
            aq_commands=[],
            plugin_tools=[],
        )
        assert capability_policy_for(profile) == DENY_ALL

    def test_none_profile_is_deny_all(self):
        from src.profiles.capabilities import DENY_ALL, capability_policy_for

        assert capability_policy_for(None) == DENY_ALL
